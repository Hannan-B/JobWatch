"""
test_uber.py — Phase O Part 2: the Uber connector + detect + scope wiring (mocked)
==================================================================================
Verifies the Uber connector parses the real response shape, pages until a short
page, dedups multi-city roles, resolves chosen cities to coordinates, and that
detect + market_scope wire it up. Sandbox can't reach Uber, so HTTP is mocked.

Run:  python3 test_uber.py
"""

import json
from jobwatch import connectors, market_scope, detect

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def _role(jid, title, address="London, UK", team="Sales", url=None):
    return {
        "Id": jid, "Reference": jid, "Title": title,
        "Teams": [team] if team else [],
        "Locations": [{"Address": address, "City": "", "Region": "London",
                       "Country": "United Kingdom"}],
        "Urls": [{"Culture": "en-us", "Url": url or f"/en/jobs/{jid}/", "IsDefault": True}],
    }


# A mock http_get that serves pages based on the URL's page= param.
_pages_by_page = {}     # page number -> list of role dicts
_requested_urls = []
def _install_http(pages):
    global _pages_by_page, _requested_urls
    _pages_by_page = pages
    _requested_urls = []
    def fake_http_get(url, headers=None):
        _requested_urls.append(url)
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(url).query)
        page = int(q.get("page", ["1"])[0])
        roles = _pages_by_page.get(page, [])
        return json.dumps({"jobs": roles})
    connectors.http_get = fake_http_get
    connectors._polite_pause = lambda: None


def test_parses_real_shape():
    _install_http({1: [_role("157660", "Account Executive")]})
    jobs = connectors.uber({"location_list": ["London"]})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from Id", j["id"] == "157660")
    check("title parsed", j["title"] == "Account Executive")
    check("location from Address", j["location"] == "London, UK")
    check("department from Teams[0]", j["department"] == "Sales")
    check("url made absolute", j["url"] == "https://jobs.uber.com/en/jobs/157660/")


def test_location_fallback_when_address_blank():
    r = _role("1", "X")
    r["Locations"][0]["Address"] = ""   # force the fallback
    _install_http({1: [r]})
    jobs = connectors.uber({"location_list": ["London"]})
    check("falls back to Region+Country", jobs[0]["location"] == "London, United Kingdom")


def test_pages_until_short():
    # page 1 full (100), page 2 short (3) -> stop after page 2, total 103.
    p1 = [_role(str(i), "R") for i in range(100)]
    p2 = [_role(str(1000 + i), "R") for i in range(3)]
    _install_http({1: p1, 2: p2})
    jobs = connectors.uber({"location_list": ["London"]})
    check("collected 103 across 2 pages", len(jobs) == 103)
    check("requested page 1 and 2", any("page=1" in u for u in _requested_urls)
          and any("page=2" in u for u in _requested_urls))


def test_sends_coordinates_for_london():
    _install_http({1: [_role("1", "X")]})
    connectors.uber({"location_list": ["London"]})
    u = _requested_urls[0]
    check("london lat sent", "lat=51.5072178" in u)
    check("london lng sent", "lng=-0.1275862" in u)
    check("radius sent", "radius=100" in u)


def test_unknown_city_falls_back_to_broad():
    # A city not in CITY_COORDS -> no targets -> broad fetch (no lat/lng).
    _install_http({1: [_role("1", "X")]})
    connectors.uber({"location_list": ["Atlantis"]})
    u = _requested_urls[0]
    check("no lat when city unknown", "lat=" not in u)


def test_dedupes_multi_city():
    # Same role id returned under two cities -> collapsed to one, locations merged.
    # (Add a second city to CITY_COORDS at runtime for the test.)
    import jobwatch.connectors as c
    r_london = _role("same", "Dup", address="London, UK")
    r_dublin = _role("same", "Dup", address="Dublin, Ireland")
    # Serve different roles depending on the lat in the URL.
    def fake_http_get(url, headers=None):
        _requested_urls.append(url)
        return json.dumps({"jobs": [r_dublin if "lat=53" in url else r_london]})
    c.http_get = fake_http_get
    c._polite_pause = lambda: None
    # Temporarily add Dublin coords.
    # We can't edit the closure table, so test dedupe via _merge_by_id directly:
    merged = c._merge_by_id([
        {"id": "same", "title": "Dup", "location": "London, UK", "department": "", "url": ""},
        {"id": "same", "title": "Dup", "location": "Dublin, Ireland", "department": "", "url": ""},
    ])
    check("multi-city role merged to one", len(merged) == 1)
    check("locations merged", merged[0]["location"] == "London, UK; Dublin, Ireland")


def test_detect_recognises_uber():
    for url in ["https://jobs.uber.com/en/jobs/",
                "https://jobs.uber.com/en/jobs/?location=London",
                "https://www.uber.com/gb/en/careers/list/"]:
        r = detect.detect(url)
        check(f"detect uber: {url[:40]}", r["provider"] == "uber")


def test_market_scope_passes_cities_to_uber():
    cfg = market_scope.scoped_config("uber", {}, ["London", "Dublin"])
    check("uber gets city names (not codes)",
          cfg.get("location_list") == ["London", "Dublin"])
    # No cities -> unchanged (broad fetch).
    cfg2 = market_scope.scoped_config("uber", {}, [])
    check("uber no cities -> no location_list", "location_list" not in cfg2)


def test_uber_in_connectors_registry():
    check("uber registered", "uber" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["uber"]
    check("uber needs no required config", required == [])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
