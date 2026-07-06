"""
test_playstation.py — PlayStation connector + detect + scope wiring (mocked)
============================================================================
Verifies the PlayStation connector parses the real get-jobs response shape,
reads the CITY (not country alone), pulls department from the
cf_job_family_group custom field, pages by page_number to the reported
totalJob, retries a failed page once, dedupes multi-city roles, turns chosen
cities into filter[city] params, and that detect + market_scope wire it up.
Sandbox can't reach PlayStation, so HTTP is mocked.

Run:  python3 test_playstation.py
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


def _role(jid, title="Audio Programmer", city="London", state="England",
          country="United Kingdom", dept="Engineering", url=None):
    """Build one role in PlayStation's real captured shape."""
    return {
        "requisitionID": jid,
        "reference": jid,
        "uniqueID": jid + "U",
        "title": title,
        "locations": [{
            "city": city, "state": state, "country": country,
            "countryAbbr": "GB",
            "locationText": f"1 Any Street, {city}, {country}",
        }],
        "customFields": [
            {"cfKey": "cf_min_value", "value": "1.0", "key": "Min Value"},
            {"cfKey": "cf_job_family_group", "value": dept, "key": "Job Family Group"},
        ],
        "applyURL": url or f"https://job-boards.greenhouse.io/sonyinteractiveentertainmentglobal/jobs/{jid}",
    }


# A mock opener that serves pages based on the URL's page_number param. The
# connector now warms a session (GET home) then POSTs each page on one opener,
# so we replace urllib.request.build_opener with a fake opener.
_pages_by_page = {}     # page number -> {"jobs": [...], "totalJob": N}
_requested_urls = []
def _install_http(pages):
    global _pages_by_page, _requested_urls
    _pages_by_page = pages
    _requested_urls = []

    class _Resp:
        def __init__(self, payload): self._p = payload
        def read(self): return self._p.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _FakeOpener:
        def open(self, req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            # The warm-up GET hits the home page — serve empty HTML.
            if "/api/get-jobs" not in url:
                return _Resp("<html></html>")
            _requested_urls.append(url)
            import urllib.parse as up
            q = up.parse_qs(up.urlparse(url).query)
            page = int(q.get("page_number", ["1"])[0])
            return _Resp(json.dumps(_pages_by_page.get(page, {"jobs": [], "totalJob": 0})))

    connectors.urllib.request.build_opener = lambda *a, **k: _FakeOpener()
    connectors._polite_pause = lambda: None


def test_parses_real_shape():
    _install_http({1: {"jobs": [_role("R-1")], "totalJob": 1}})
    jobs = connectors.playstation({"location_list": ["London"]})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from requisitionID", j["id"] == "R-1")
    check("title parsed", j["title"] == "Audio Programmer")
    check("city captured (not country alone)", j["location"] == "London, England, United Kingdom")
    check("department from cf_job_family_group", j["department"] == "Engineering")
    check("url from applyURL", j["url"].endswith("/jobs/R-1"))


def test_department_blank_when_missing():
    r = _role("R-2")
    r["customFields"] = [{"cfKey": "cf_unit", "value": "GBP", "key": "Unit"}]
    _install_http({1: {"jobs": [r], "totalJob": 1}})
    jobs = connectors.playstation({})
    check("department blank when no cf_job_family_group", jobs[0]["department"] == "")


def test_pages_to_total():
    # totalJob=5: page1 has 3, page2 has 2 -> stop after page2 (5 collected).
    p1 = {"jobs": [_role(f"R-{i}") for i in range(3)], "totalJob": 5}
    p2 = {"jobs": [_role(f"R-{10+i}") for i in range(2)], "totalJob": 5}
    _install_http({1: p1, 2: p2})
    jobs = connectors.playstation({})
    check("collected 5 across 2 pages", len(jobs) == 5)
    check("requested page 1 and 2",
          any("page_number=1" in u for u in _requested_urls)
          and any("page_number=2" in u for u in _requested_urls))
    check("did NOT request page 3", not any("page_number=3" in u for u in _requested_urls))


def test_retries_failed_page_once():
    # page 1 POST fails on first attempt, succeeds on retry.
    global _requested_urls
    _requested_urls = []
    state = {"posts": 0}

    class _Resp:
        def __init__(self, payload): self._p = payload
        def read(self): return self._p.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _FlakyOpener:
        def open(self, req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/api/get-jobs" not in url:
                return _Resp("<html></html>")
            state["posts"] += 1
            if state["posts"] == 1:
                raise ConnectionError("transient")
            _requested_urls.append(url)
            return _Resp(json.dumps({"jobs": [_role("R-9")], "totalJob": 1}))

    connectors.urllib.request.build_opener = lambda *a, **k: _FlakyOpener()
    connectors._polite_pause = lambda: None
    jobs = connectors.playstation({})
    check("page retried once and succeeded", len(jobs) == 1)
    check("two POST attempts made for page 1", state["posts"] == 2)


def test_city_filter_sent_for_chosen_cities():
    _install_http({1: {"jobs": [_role("R-1")], "totalJob": 1}})
    connectors.playstation({"location_list": ["London", "Berlin"]})
    u = _requested_urls[0]
    check("filter[city][0]=London sent", "filter%5Bcity%5D%5B0%5D=London" in u
          or "filter[city][0]=London" in u)
    check("filter[city][1]=Berlin sent", "filter%5Bcity%5D%5B1%5D=Berlin" in u
          or "filter[city][1]=Berlin" in u)
    check("page_number sent", "page_number=1" in u)


def test_no_city_filter_when_no_cities():
    _install_http({1: {"jobs": [_role("R-1")], "totalJob": 1}})
    connectors.playstation({})
    u = _requested_urls[0]
    check("no city filter when no cities", "filter" not in u)


def test_dedupes_multi_city():
    # Same role id under two city queries -> collapsed to one, locations merged.
    merged = connectors._merge_by_id([
        {"id": "same", "title": "X", "location": "London, England, United Kingdom",
         "department": "", "url": ""},
        {"id": "same", "title": "X", "location": "Berlin, Germany",
         "department": "", "url": ""},
    ])
    check("multi-city role merged to one", len(merged) == 1)
    check("locations merged",
          merged[0]["location"] == "London, England, United Kingdom; Berlin, Germany")


def test_detect_recognises_playstation():
    for url in ["https://careers.playstation.com/#jobs-list-section",
                "https://careers.playstation.com/api/get-jobs?page_number=1"]:
        r = detect.detect(url)
        check(f"detect playstation: {url[:45]}", r["provider"] == "playstation")


def test_market_scope_passes_cities_to_playstation():
    cfg = market_scope.scoped_config("playstation", {}, ["London", "Berlin"])
    check("playstation gets city names (not codes)",
          cfg.get("location_list") == ["London", "Berlin"])
    cfg2 = market_scope.scoped_config("playstation", {}, [])
    check("playstation no cities -> no location_list", "location_list" not in cfg2)


def test_playstation_in_connectors_registry():
    check("playstation registered", "playstation" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["playstation"]
    check("playstation needs no required config", required == [])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
