"""
test_filters_london_boroughs.py — London borough → London location mapping
===========================================================================
Several boards (Teamtailor tenants like Cazoo and Chip) tag roles by the specific
London BOROUGH or district ("Richmond upon Thames", "Shoreditch") rather than
"London". Pure city-text matching would drop those from a "London" filter even
though they ARE in Greater London. filters._SITE_CITY maps the 32 official
boroughs (+ confirmed areas / HQ labels) to "london" so they resolve, tagged
location_via_site.

This guards two things at once:
  1. real borough/area tags resolve to London (matched, via_site), and
  2. same-named places elsewhere (Richmond VA, Kingston Jamaica) are NOT pulled
     in — the map is exact-string on FULL borough names, never bare "richmond".

Run:  python3 test_filters_london_boroughs.py
"""

from jobwatch import filters

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def _job(loc, title="Role"):
    return {"id": loc, "title": title, "location": loc, "department": "", "url": ""}


def _matched_locs(res):
    return {j["location"] for j in res["matched"]}


def test_confirmed_board_tags_resolve_to_london():
    # The exact strings seen live on Cazoo and Chip.
    jobs = [_job("Richmond upon Thames"), _job("Shoreditch"), _job("Chip HQ")]
    res = filters.filter_by_location(jobs, ["London"])
    locs = _matched_locs(res)
    check("Richmond upon Thames -> London", "Richmond upon Thames" in locs)
    check("Shoreditch -> London", "Shoreditch" in locs)
    check("Chip HQ -> London", "Chip HQ" in locs)
    check("all three matched", len(res["matched"]) == 3)
    check("none dropped to elsewhere", res["elsewhere"] == [])


def test_via_site_flag_set():
    res = filters.filter_by_location([_job("Hackney")], ["London"])
    check("borough matched", len(res["matched"]) == 1)
    check("carries location_via_site flag",
          res["matched"][0].get("location_via_site") is True)


def test_all_32_boroughs_resolve():
    boroughs = [
        "City of London", "Barking and Dagenham", "Barnet", "Bexley", "Brent",
        "Bromley", "Camden", "Croydon", "Ealing", "Enfield", "Greenwich",
        "Hackney", "Hammersmith and Fulham", "Haringey", "Harrow", "Havering",
        "Hillingdon", "Hounslow", "Islington", "Kensington and Chelsea",
        "Kingston upon Thames", "Lambeth", "Lewisham", "Merton", "Newham",
        "Redbridge", "Richmond upon Thames", "Southwark", "Sutton",
        "Tower Hamlets", "Waltham Forest", "Wandsworth", "Westminster",
    ]
    jobs = [_job(b) for b in boroughs]
    res = filters.filter_by_location(jobs, ["London"])
    missed = [j["location"] for j in res["elsewhere"]] + \
             [j["location"] for j in res["ambiguous"]]
    check(f"all {len(boroughs)} boroughs resolve to London (missed: {missed})",
          len(res["matched"]) == len(boroughs))


def test_case_insensitive():
    res = filters.filter_by_location(
        [_job("richmond upon thames"), _job("SHOREDITCH")], ["London"])
    check("borough match is case-insensitive", len(res["matched"]) == 2)


def test_ambiguous_same_name_places_not_pulled_in():
    # The whole point of using FULL borough names: these must stay OUT of London.
    jobs = [_job("Richmond, VA"), _job("Kingston, Jamaica"), _job("Manchester")]
    res = filters.filter_by_location(jobs, ["London"])
    out = {j["location"] for j in res["elsewhere"]}
    check("Richmond VA not matched as London", "Richmond, VA" in out)
    check("Kingston Jamaica not matched as London", "Kingston, Jamaica" in out)
    check("Manchester not matched as London", "Manchester" in out)
    check("nothing wrongly matched", res["matched"] == [])


def test_plain_london_still_matches():
    res = filters.filter_by_location([_job("London")], ["London"])
    check("plain 'London' still matches directly", len(res["matched"]) == 1)
    check("direct match is NOT flagged via_site",
          not res["matched"][0].get("location_via_site"))


def test_national_gallery_still_resolves():
    # Pre-existing _SITE_CITY entry must be unaffected by the additions.
    res = filters.filter_by_location([_job("The National Gallery")], ["London"])
    check("National Gallery still -> London", len(res["matched"]) == 1)


def test_non_london_site_not_affected():
    res = filters.filter_by_location([_job("Edinburgh"), _job("Bristol")], ["London"])
    check("Edinburgh/Bristol stay elsewhere", len(res["elsewhere"]) == 2)


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
