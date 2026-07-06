"""
market_scope.py  (Phase O — source-side fetch scoping from your chosen cities)
==============================================================================
THE PROBLEM this solves (proven by two probes on the user's Mac):
  The big custom boards return the WHOLE WORLD's jobs. Apple = 6411 globally,
  capped at 2000 by the old connector; Google = thousands. We then throw ~98%
  away to keep London. That's slow (minutes) and — because of the cap — INCOMPLETE
  (London roles that sort after the cap were never fetched; the real Apple-UK
  count is ~118, but the capped global fetch only surfaced 27).

THE FIX (locked model — "cities drive everything"):
  You pick CITIES (your existing locations_allowed). Each city resolves to its
  COUNTRY via geo.py, and that country's code scopes the fetch AT THE SOURCE:
    - Apple wants ISO-3 in a "postLocation-GBR" code  (probe-confirmed: 6411->118)
    - Google wants ISO-2 ("GB") in its location slot   (probe-confirmed working)
  So instead of fetching the planet, Apple fetches UK (~118), fast and COMPLETE.
  The app's location filter still runs and narrows UK -> London. Scoping only
  changes WHICH roles get fetched; it NEVER decides what's shown (that stays
  filters.filter_by_location, unchanged).

THE RULE (locked — "Option A", all-or-fetch-all):
  Scope a board ONLY when EVERY chosen city resolves to a region. If ANY chosen
  city is unknown to geo (can't resolve), that board fetches EVERYTHING for that
  run (unscoped) so the unknown city's roles are never silently dropped. Empty
  locations -> fetch everything (today's behaviour). This is safe by construction:
  an unresolvable city can only ever WIDEN the fetch, never narrow past a role.
  For the user's real cities (London, New York, Dublin...) every city resolves,
  so the fallback never fires — it's just the honest safety net.

WHAT THIS MODULE DOES NOT DO:
  - It does not fetch anything (connectors do).
  - It does not filter anything (filters.py does, unchanged).
  - It does not touch snapshots/compare/trends.
  It is a pure function from (chosen cities) -> (per-board config additions).

Only Apple and Google are SCOPABLE today. Every other connector (Greenhouse,
Lever, Ashby, Workday, Pinpoint, ...) already returns a small, city-tagged list,
so they ignore scoping entirely and fetch as before.
"""

from . import geo


# Which connectors can be scoped at the source, and how each wants its scope.
# kind tells us which ISO form to use and where to put it.
#   apple  -> config["location"] = "postLocation-<ISO3>"   (one code; see note)
#   google -> config["location"] = "<ISO2>"                (Google's location slot)
# Boards not listed here are NOT scopable: they fetch everything (already small).
SCOPABLE = {"apple", "google", "uber", "spotify", "playstation"}


class _ScopeResult:
    """Internal: the outcome of resolving a set of cities for one run."""
    __slots__ = ("regions", "all_resolved", "unresolved")

    def __init__(self, regions, all_resolved, unresolved):
        self.regions = regions            # list of region dicts (geo.region_for_city)
        self.all_resolved = all_resolved  # True if EVERY chosen city resolved
        self.unresolved = unresolved      # list of city strings that didn't resolve


def resolve_cities(locations_allowed):
    """
    Resolve a list of chosen city strings to their regions.

    Returns _ScopeResult with:
      .regions       - region dicts for the cities that DID resolve
      .all_resolved  - True only if every non-blank chosen city resolved
      .unresolved    - the city strings that couldn't be resolved (for the warning)

    An empty/blank input yields all_resolved=False with no regions (so callers
    treat "no cities" as "don't scope" — fetch everything, today's behaviour).
    """
    cities = [str(c).strip() for c in (locations_allowed or []) if str(c).strip()]
    if not cities:
        return _ScopeResult([], False, [])

    g = geo.load_geo()
    regions, unresolved = [], []
    for c in cities:
        r = geo.region_for_city(c, g)
        if r is None:
            unresolved.append(c)
        else:
            regions.append(r)
    return _ScopeResult(regions, len(unresolved) == 0, unresolved)


def _apple_scope(regions):
    """Apple's location code(s) for the resolved regions.

    Probe-confirmed: Apple accepts ONE working format, 'postLocation-<ISO3>'
    (e.g. 'postLocation-GBR' = UK, 6411->118). City-level codes return 0, so we
    only ever scope to COUNTRY. The app filter narrows to the city afterwards.

    Apple's search body takes filters.locations as a LIST, so multiple countries
    (London + New York -> GBR + USA) are passed together. We de-dupe by ISO-3.
    """
    seen, codes = set(), []
    for r in regions:
        iso3 = r.get("iso3")
        if iso3 and iso3 not in seen:
            seen.add(iso3)
            codes.append(f"postLocation-{iso3}")
    return codes


def _google_scope(regions):
    """Google's location value(s) for the resolved regions — ISO-2 country codes,
    de-duped (e.g. London + Dublin -> GB + IE)."""
    seen, codes = set(), []
    for r in regions:
        iso2 = r.get("iso2")
        if iso2 and iso2 not in seen:
            seen.add(iso2)
            codes.append(iso2)
    return codes


def scoped_config(connector, base_config, locations_allowed):
    """
    Return a COPY of base_config, augmented with a source-side location scope
    derived from the chosen cities — when (and only when) scoping is both possible
    and safe (Option A). Otherwise returns base_config unchanged (fetch everything).

    connector        - the company's connector name (e.g. "apple", "google",
                       "greenhouse"). Only apple/google are scopable.
    base_config      - the company's existing config dict (untouched; we copy).
    locations_allowed- the run's chosen cities (from interests/run override).

    Decision table:
      * connector not scopable        -> base_config unchanged.
      * no cities chosen              -> base_config unchanged (fetch all).
      * some chosen city unresolved   -> base_config unchanged (Option A safety:
                                         fetch all so the unknown city isn't lost).
      * all cities resolve            -> add the board's scope codes to a copy.

    Apple: sets config["location"] only if not already explicitly set by the user
    (an explicit preset code wins — we never override a deliberate choice).
    Google: same — respect an explicit config["location"] if present.
    """
    conn = (connector or "").lower()
    if conn not in SCOPABLE:
        return base_config

    # Uber and Spotify are city-name scoped (not country-scoped): each resolves
    # the chosen city NAMES itself (Uber → coords; Spotify → location slugs), so
    # they just need the raw chosen cities passed through and bypass the Option-A
    # country gate. A city they can't handle is dealt with inside the connector;
    # with no cities they do a broad fetch.
    if conn in ("uber", "spotify", "playstation"):
        cities = [str(c).strip() for c in (locations_allowed or []) if str(c).strip()]
        if not cities:
            return base_config
        cfg = dict(base_config or {})
        if str(cfg.get("location", "")).strip():
            return cfg
        cfg["location_list"] = cities
        return cfg

    res = resolve_cities(locations_allowed)
    # No cities, or any city unresolved -> don't scope (fetch everything). Safe.
    if not res.regions or not res.all_resolved:
        return base_config

    cfg = dict(base_config or {})
    # Respect an explicit, user-set location code — never override a deliberate one.
    if str(cfg.get("location", "")).strip():
        return cfg

    if conn == "apple":
        codes = _apple_scope(res.regions)
        if codes:
            # Apple's connector reads a single config["location"] today; we pass
            # the first country code there. (The connector change in step 2 will
            # accept a list for multi-country; until then one country is the
            # common case and correct for a single-city filter.)
            cfg["location"] = codes[0]
            cfg["location_list"] = codes  # forward-compat: connector may read this
    elif conn == "google":
        codes = _google_scope(res.regions)
        if codes:
            cfg["location"] = codes[0]
            cfg["location_list"] = codes
    return cfg


def describe_scope(connector, locations_allowed):
    """Plain-language summary of what scoping WOULD do for this connector + cities,
    for the terminal test and any future 'fetching: UK' caption. Never raises."""
    conn = (connector or "").lower()
    if conn not in SCOPABLE:
        return f"{conn or '(none)'}: not scopable — fetches its normal list."
    cities = [str(c).strip() for c in (locations_allowed or []) if str(c).strip()]
    if conn in ("uber", "spotify", "playstation"):
        if not cities:
            return f"{conn}: no cities chosen — broad fetch, then app-filters."
        return (f"{conn}: fetches by city name for {', '.join(cities)} "
                f"(cities it can resolve; others handled in-connector) — then app-filters.")
    res = resolve_cities(locations_allowed)
    if not res.regions and not res.unresolved:
        return f"{conn}: no cities chosen — fetches EVERYTHING, then app-filters."
    if not res.all_resolved:
        unknown = ", ".join(res.unresolved)
        return (f"{conn}: '{unknown}' not recognised — fetches EVERYTHING "
                f"(safe fallback), then app-filters.")
    countries = ", ".join(sorted({r["country"].title() for r in res.regions}))
    if conn == "apple":
        codes = ", ".join(_apple_scope(res.regions))
    else:
        codes = ", ".join(_google_scope(res.regions))
    return f"{conn}: scopes to {countries} (codes: {codes}) — fast & complete."


# Quick manual test:  python3 -m jobwatch.market_scope London "New York"
if __name__ == "__main__":
    import sys
    cities = sys.argv[1:] or ["London"]
    print(f"Chosen cities: {cities}\n")
    res = resolve_cities(cities)
    print(f"all_resolved = {res.all_resolved}")
    if res.unresolved:
        print(f"unresolved   = {res.unresolved}")
    for r in res.regions:
        print(f"  {r['city']:18} -> {r['country'].title():18} "
              f"ISO2={r['iso2']} ISO3={r['iso3']}"
              + (f" state={r['state_code'].upper()}" if r.get('state_code') else ""))
    print()
    for conn in ("apple", "google", "greenhouse"):
        print("  " + describe_scope(conn, cities))
