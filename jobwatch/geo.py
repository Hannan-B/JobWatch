"""
geo.py  (city -> country/state map; OFFLINE, bundled dataset)
============================================================
The locked rule is: location filtering is CITY-focused. But some boards report
only the COUNTRY (Google Careers gives country-level codes; some Apple roles say
just "United Kingdom"). With pure city text-matching those country-only roles
never match a city filter and get lost (the real bug: Google showed 24 of 104
London roles; the other 80 were tagged only "United Kingdom").

Fix (user-locked): when you filter by a CITY, a role tagged only at the COUNTRY
level of that city counts as a MATCH, but a clearly TAGGED one ("city not
specified"), never disguised as a confirmed-city hit. To do that we need to know
which country (and, in the US, which state) a city sits in.

DATA SOURCE - fully offline, no web calls ever:
    jobwatch/data/cities.json
generated once from the GeoNames dataset (via the geonamescache package) by the
dev tool build_geo_data.py. ~11k cities worldwide (pop >= 50k) plus common
aliases ("new york", "nyc", "dubai", ...). The app reads this file and NEVER
touches the network - per the user's instruction: get a real database, bake it
in, run offline, don't keep checking the web. To refresh, re-run build_geo_data
and ship the new cities.json.

GRANULARITY: UK roles are "London, United Kingdom" or just "United Kingdom" ->
city->country suffices. US roles are "New York, NY" / just "United States" -> we
need city->STATE->country. So US entries carry state + state_code; others omit.

AMBIGUITY: same name in many countries ("London" GB and CA). The bundled data
keeps the HIGHEST-POPULATION city per name (London->GB), almost always the one
meant.

This never changes what gets fetched or recorded (location stays view-layer). It
only helps the MATCHER. Forgiving: a missing/corrupt data file falls back to a
tiny built-in map, never fatal.
"""

import json
from pathlib import Path


# Tiny built-in fallback if the bundled cities.json is missing/corrupt.
_FALLBACK = {
    "london":   {"country": "united kingdom",
                 "country_aliases": ["uk", "england", "great britain", "gb"]},
    "new york": {"country": "united states",
                 "country_aliases": ["usa", "us", "u.s.", "u.s.a.", "america"],
                 "state": "new york", "state_code": "ny"},
}

_DATA_FILE = Path(__file__).parent / "data" / "cities.json"
_CACHE = None


def _load_file() -> dict:
    try:
        data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_FALLBACK)


def load_geo() -> dict:
    """City -> {country, country_aliases, state?, state_code?}. Lowercased keys.
    Loaded once from the bundled dataset, cached. Always usable (offline-safe)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_file()
    return _CACHE


def country_terms_for_city(city: str, geo: dict | None = None) -> list:
    """Normalized COUNTRY-level terms a country-only role in this city's country
    could carry, so the matcher can treat such a role as a (tagged) match.
    [] when the city is unknown (no fallback -> city stays strict). US cities
    also yield STATE + STATE CODE (boards tag "New York, NY")."""
    g = geo if geo is not None else load_geo()
    entry = g.get((city or "").lower().strip())
    if not entry:
        return []
    terms = []
    country = (entry.get("country") or "").lower().strip()
    if country:
        terms.append(country)
    for a in entry.get("country_aliases", []):
        a = (a or "").lower().strip()
        if a:
            terms.append(a)
    st = (entry.get("state") or "").lower().strip()
    if st:
        terms.append(st)
    sc = (entry.get("state_code") or "").lower().strip()
    if sc:
        terms.append(sc)
    return terms


if __name__ == "__main__":
    g = load_geo()
    print(f"geo cities known: {len(g)}")
    for city in ("London", "New York", "Dubai", "Singapore", "Cupertino", "Nowhereville"):
        print(f"  {city!r:14} -> {country_terms_for_city(city, g)}")


# ===========================================================================
# Phase O — city -> ISO country code, for source-side fetch scoping
# ===========================================================================
# The locked Phase-O model: the user picks CITIES; each city resolves to its
# COUNTRY here, and that country's code scopes the Apple/Google fetch (Apple
# wants ISO-3 "GBR", Google wants ISO-2 "GB"). This is the inverse use of the
# same offline cities.json: city -> country name (already loaded) -> ISO code
# (this static map). Fully offline, no web calls — same rule as the rest of geo.
#
# Covers every country NAME that appears in the bundled cities.json (verified
# 187/187). A country we somehow don't have an ISO for just yields no code, so
# that city can't scope a fetch — which, under the locked Option-A rule, means
# the board fetches everything and the app filter still narrows it. Safe.

COUNTRY_ISO = {
    "afghanistan": ("AF", "AFG"), "albania": ("AL", "ALB"), "algeria": ("DZ", "DZA"),
    "angola": ("AO", "AGO"), "argentina": ("AR", "ARG"), "armenia": ("AM", "ARM"),
    "australia": ("AU", "AUS"), "austria": ("AT", "AUT"), "azerbaijan": ("AZ", "AZE"),
    "bahamas": ("BS", "BHS"), "bahrain": ("BH", "BHR"), "bangladesh": ("BD", "BGD"),
    "barbados": ("BB", "BRB"), "belarus": ("BY", "BLR"), "belgium": ("BE", "BEL"),
    "belize": ("BZ", "BLZ"), "benin": ("BJ", "BEN"), "bhutan": ("BT", "BTN"),
    "bolivia": ("BO", "BOL"), "bosnia and herzegovina": ("BA", "BIH"),
    "botswana": ("BW", "BWA"), "brazil": ("BR", "BRA"), "brunei": ("BN", "BRN"),
    "bulgaria": ("BG", "BGR"), "burkina faso": ("BF", "BFA"), "burundi": ("BI", "BDI"),
    "cabo verde": ("CV", "CPV"), "cambodia": ("KH", "KHM"), "cameroon": ("CM", "CMR"),
    "canada": ("CA", "CAN"), "central african republic": ("CF", "CAF"),
    "chad": ("TD", "TCD"), "chile": ("CL", "CHL"), "china": ("CN", "CHN"),
    "colombia": ("CO", "COL"), "comoros": ("KM", "COM"), "costa rica": ("CR", "CRI"),
    "croatia": ("HR", "HRV"), "cuba": ("CU", "CUB"), "curacao": ("CW", "CUW"),
    "cyprus": ("CY", "CYP"), "czechia": ("CZ", "CZE"),
    "democratic republic of the congo": ("CD", "COD"), "denmark": ("DK", "DNK"),
    "djibouti": ("DJ", "DJI"), "dominican republic": ("DO", "DOM"),
    "ecuador": ("EC", "ECU"), "egypt": ("EG", "EGY"), "el salvador": ("SV", "SLV"),
    "equatorial guinea": ("GQ", "GNQ"), "eritrea": ("ER", "ERI"),
    "estonia": ("EE", "EST"), "eswatini": ("SZ", "SWZ"), "ethiopia": ("ET", "ETH"),
    "fiji": ("FJ", "FJI"), "finland": ("FI", "FIN"), "france": ("FR", "FRA"),
    "french guiana": ("GF", "GUF"), "gabon": ("GA", "GAB"), "gambia": ("GM", "GMB"),
    "georgia": ("GE", "GEO"), "germany": ("DE", "DEU"), "ghana": ("GH", "GHA"),
    "greece": ("GR", "GRC"), "guadeloupe": ("GP", "GLP"), "guatemala": ("GT", "GTM"),
    "guinea": ("GN", "GIN"), "guinea-bissau": ("GW", "GNB"), "guyana": ("GY", "GUY"),
    "haiti": ("HT", "HTI"), "honduras": ("HN", "HND"), "hong kong": ("HK", "HKG"),
    "hungary": ("HU", "HUN"), "iceland": ("IS", "ISL"), "india": ("IN", "IND"),
    "indonesia": ("ID", "IDN"), "iran": ("IR", "IRN"), "iraq": ("IQ", "IRQ"),
    "ireland": ("IE", "IRL"), "israel": ("IL", "ISR"), "italy": ("IT", "ITA"),
    "ivory coast": ("CI", "CIV"), "jamaica": ("JM", "JAM"), "japan": ("JP", "JPN"),
    "jordan": ("JO", "JOR"), "kazakhstan": ("KZ", "KAZ"), "kenya": ("KE", "KEN"),
    "kosovo": ("XK", "XKX"), "kuwait": ("KW", "KWT"), "kyrgyzstan": ("KG", "KGZ"),
    "laos": ("LA", "LAO"), "latvia": ("LV", "LVA"), "lebanon": ("LB", "LBN"),
    "lesotho": ("LS", "LSO"), "liberia": ("LR", "LBR"), "libya": ("LY", "LBY"),
    "lithuania": ("LT", "LTU"), "luxembourg": ("LU", "LUX"), "macao": ("MO", "MAC"),
    "madagascar": ("MG", "MDG"), "malawi": ("MW", "MWI"), "malaysia": ("MY", "MYS"),
    "maldives": ("MV", "MDV"), "mali": ("ML", "MLI"), "martinique": ("MQ", "MTQ"),
    "mauritania": ("MR", "MRT"), "mauritius": ("MU", "MUS"), "mayotte": ("YT", "MYT"),
    "mexico": ("MX", "MEX"), "moldova": ("MD", "MDA"), "mongolia": ("MN", "MNG"),
    "montenegro": ("ME", "MNE"), "morocco": ("MA", "MAR"), "mozambique": ("MZ", "MOZ"),
    "myanmar": ("MM", "MMR"), "namibia": ("NA", "NAM"), "nepal": ("NP", "NPL"),
    "new caledonia": ("NC", "NCL"), "new zealand": ("NZ", "NZL"),
    "nicaragua": ("NI", "NIC"), "niger": ("NE", "NER"), "nigeria": ("NG", "NGA"),
    "north korea": ("KP", "PRK"), "north macedonia": ("MK", "MKD"),
    "norway": ("NO", "NOR"), "oman": ("OM", "OMN"), "pakistan": ("PK", "PAK"),
    "palestinian territory": ("PS", "PSE"), "panama": ("PA", "PAN"),
    "papua new guinea": ("PG", "PNG"), "paraguay": ("PY", "PRY"), "peru": ("PE", "PER"),
    "philippines": ("PH", "PHL"), "poland": ("PL", "POL"), "portugal": ("PT", "PRT"),
    "puerto rico": ("PR", "PRI"), "qatar": ("QA", "QAT"),
    "republic of the congo": ("CG", "COG"), "reunion": ("RE", "REU"),
    "romania": ("RO", "ROU"), "russia": ("RU", "RUS"), "rwanda": ("RW", "RWA"),
    "sao tome and principe": ("ST", "STP"), "saudi arabia": ("SA", "SAU"),
    "senegal": ("SN", "SEN"), "serbia": ("RS", "SRB"), "sierra leone": ("SL", "SLE"),
    "singapore": ("SG", "SGP"), "slovakia": ("SK", "SVK"), "slovenia": ("SI", "SVN"),
    "solomon islands": ("SB", "SLB"), "somalia": ("SO", "SOM"),
    "south africa": ("ZA", "ZAF"), "south korea": ("KR", "KOR"),
    "south sudan": ("SS", "SSD"), "spain": ("ES", "ESP"), "sri lanka": ("LK", "LKA"),
    "sudan": ("SD", "SDN"), "suriname": ("SR", "SUR"), "sweden": ("SE", "SWE"),
    "switzerland": ("CH", "CHE"), "syria": ("SY", "SYR"), "taiwan": ("TW", "TWN"),
    "tajikistan": ("TJ", "TJK"), "tanzania": ("TZ", "TZA"), "thailand": ("TH", "THA"),
    "the netherlands": ("NL", "NLD"), "timor leste": ("TL", "TLS"), "togo": ("TG", "TGO"),
    "trinidad and tobago": ("TT", "TTO"), "tunisia": ("TN", "TUN"), "turkey": ("TR", "TUR"),
    "turkmenistan": ("TM", "TKM"), "u.s. virgin islands": ("VI", "VIR"),
    "uganda": ("UG", "UGA"), "ukraine": ("UA", "UKR"),
    "united arab emirates": ("AE", "ARE"), "united kingdom": ("GB", "GBR"),
    "united states": ("US", "USA"), "uruguay": ("UY", "URY"),
    "uzbekistan": ("UZ", "UZB"), "venezuela": ("VE", "VEN"), "vietnam": ("VN", "VNM"),
    "western sahara": ("EH", "ESH"), "yemen": ("YE", "YEM"), "zambia": ("ZM", "ZMB"),
    "zimbabwe": ("ZW", "ZWE"),
    # A few alias spellings boards/users use, mapped to the same codes.
    "netherlands": ("NL", "NLD"), "holland": ("NL", "NLD"), "uae": ("AE", "ARE"),
    "uk": ("GB", "GBR"), "great britain": ("GB", "GBR"), "usa": ("US", "USA"),
    "czech republic": ("CZ", "CZE"), "south-korea": ("KR", "KOR"),
}


def iso_codes_for_country(country: str):
    """(iso2, iso3) for a country NAME (case-insensitive), or None if unknown."""
    return COUNTRY_ISO.get((country or "").lower().strip())


def region_for_city(city: str, geo: dict | None = None):
    """
    Resolve a CITY (as the user typed it) to its fetch REGION, for source-side
    scoping. Returns a dict, or None when the city is unknown to geo (which, under
    the locked Option-A rule, makes scopable boards fetch everything for that run).

        {
          "city":       "London",            # the input, trimmed
          "country":    "united kingdom",    # geo's country name for the city
          "iso2":       "GB",                # for Google
          "iso3":       "GBR",               # for Apple (postLocation-GBR)
          "state":      "new york" | None,   # US only (from cities.json)
          "state_code": "ny" | None,         # US only
        }

    None is returned when: the city isn't in cities.json at all, OR it is but its
    country has no ISO code (shouldn't happen — all 187 are covered). Either way,
    "no region" is the honest signal the scoper needs.
    """
    g = geo if geo is not None else load_geo()
    name = (city or "").lower().strip()
    entry = g.get(name)
    if not entry:
        return None
    country = (entry.get("country") or "").lower().strip()
    iso = iso_codes_for_country(country)
    if not iso:
        return None
    iso2, iso3 = iso
    return {
        "city": (city or "").strip(),
        "country": country,
        "iso2": iso2,
        "iso3": iso3,
        "state": (entry.get("state") or None),
        "state_code": (entry.get("state_code") or None),
    }
