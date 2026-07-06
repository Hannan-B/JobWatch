#!/usr/bin/env python3
"""
build_geo_data.py  (dev/build tool — NOT shipped, run once to regenerate data)
==============================================================================
Converts the bundled `geonamescache` dataset into JobWatch's compact, OFFLINE
city -> {country, country_aliases, state, state_code} map, written as
    jobwatch/data/cities.json

Design choices:
- CITY-FOCUSED (the locked rule): keyed by lowercased city name.
- Country stored as the DISPLAY NAME boards use ("United Kingdom"), with common
  aliases ("uk", "england", ...) so country-only board text still matches.
- US cities additionally carry state + state_code, because US boards tag at
  state level ("New York, NY"). Other countries omit state.
- AMBIGUITY: the same city name exists in many countries ("London" in GB and CA;
  "San Francisco" in 6). We keep the HIGHEST-POPULATION city for each name —
  almost always the one a job-seeker means. (London->GB 8.9M beats London->CA.)
- SIZE: include cities with population >= MIN_POP so the file covers every
  realistic job market worldwide without shipping all 32k rows.

This runs in the build environment (where geonamescache is installed). The app
itself NEVER imports geonamescache or touches the web — it only reads the
generated cities.json. Re-run this to refresh the bundled data.
"""

import json
from pathlib import Path

import geonamescache

MIN_POP = 50000          # covers essentially every city anyone job-hunts in
OUT = Path(__file__).parent / "jobwatch" / "data" / "cities.json"

# Country-name aliases the boards actually use (beyond the canonical name).
COUNTRY_ALIASES = {
    "United Kingdom": ["uk", "england", "scotland", "wales", "great britain", "gb", "u.k."],
    "United States": ["usa", "us", "u.s.", "u.s.a.", "america"],
    "United Arab Emirates": ["uae", "u.a.e."],
    "Ireland": ["eire"],
    "Germany": ["deutschland"],
    "Netherlands": ["holland"],
}


def build():
    gc = geonamescache.GeonamesCache()
    countries = gc.get_countries()       # code -> {name, ...}
    us_states = gc.get_us_states()       # code -> {name, ...}
    cities = gc.get_cities()             # id -> {name, countrycode, admin1code, population}

    # name(lower) -> (population, entry) so we can keep the biggest per name.
    best = {}
    for c in cities.values():
        pop = c.get("population") or 0
        if pop < MIN_POP:
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        cc = c.get("countrycode") or ""
        country = countries.get(cc, {}).get("name", "")
        if not country:
            continue
        entry = {
            "country": country.lower(),
            "country_aliases": [a.lower() for a in COUNTRY_ALIASES.get(country, [])],
        }
        # US state granularity.
        if cc == "US":
            sc = (c.get("admin1code") or "").strip()
            st = us_states.get(sc, {}).get("name", "")
            if st:
                entry["state"] = st.lower()
                entry["state_code"] = sc.lower()

        prev = best.get(key)
        if prev is None or pop > prev[0]:
            best[key] = (pop, entry)

    out = {name: entry for name, (pop, entry) in best.items()}

    # Common name aliases people actually type -> the dataset's canonical key.
    # (The dataset uses "new york city"; users type "new york" / "nyc".)
    CITY_ALIASES = {
        "new york": "new york city",
        "nyc": "new york city",
        "sf": "san francisco",
        "la": "los angeles",
        "greater london": "london",
        "city of london": "london",
        "washington dc": "washington",
        "washington d.c.": "washington",
    }
    for alias, canonical in CITY_ALIASES.items():
        if canonical in out and alias not in out:
            out[alias] = out[canonical]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"Wrote {len(out)} cities to {OUT}")
    # Sanity spot-checks.
    for c in ("london", "new york city", "dubai", "singapore", "cupertino", "berlin"):
        print(f"  {c!r:18} -> {out.get(c)}")


if __name__ == "__main__":
    build()
