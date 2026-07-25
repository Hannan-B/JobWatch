"""
test_webitrent.py — MHR iTrent (webitrent) connector + detect wiring (mocked)
=============================================================================
Verifies the webitrent connector warms an anonymous session (extracts USESSION
from the launch page), calls the etrec106gf.json list endpoint, parses the real
response shape (vacancy_id / job_title / location_id), unescapes entities, leaves
department blank (the feed has none), builds the advert URL, paginates to
total_rec, dedupes, and that detect recognises a webitrent launch URL and pulls
the WVID. Sandbox can't reach webitrent, so HTTP is mocked.

Run:  python3 test_webitrent.py
"""

import json
from jobwatch import connectors, detect

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


LAUNCH = ("https://ce0838li.webitrent.com/ce0838li_webrecruitment/wrd/run/"
          "ETREC179GF.open?WVID=7102672HeH")
_SES = "3B9273C0360FFCE5EDD87A698AC74EAC"


def _role(vid, title, location="The National Gallery"):
    return {"vacancy_id": vid, "job_title": title, "location_id": location,
            "salary": "£1 per annum", "vacancy_ref": "REQ" + vid,
            "basis_id": "Full Time", "app_close_d": "20260726"}


_pages = []          # list of {"search":..., "results":[...]} served in order
_requested = []
_req_headers = []
_launch_html = f'<html><script>var s="USESSION={_SES}";</script></html>'

def _install(pages, launch_html=None):
    global _pages, _requested, _req_headers, _launch_html
    _pages = pages
    _requested = []
    _req_headers = []
    _launch_html = launch_html if launch_html is not None else \
        f'<html><script>var s="USESSION={_SES}";</script></html>'

    class _Resp:
        # NOTE: the connector reads r.geturl() on the launch response to gather
        # USESSION candidates from the FINAL (post-redirect) url, so the fake
        # response must expose it or the session open raises AttributeError.
        def __init__(self, p, url=""): self._p = p; self._url = url
        def read(self): return self._p.encode("utf-8")
        def geturl(self): return self._url
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Op:
        def open(self, req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "ETREC179GF.open" in url and "etrec106" not in url.lower() and "VACANCY_ID" not in url:
                return _Resp(_launch_html, url)
            if "etrec106gf.json" in url:
                _requested.append(url)
                try:
                    _req_headers.append(dict(req.headers))
                except Exception:
                    _req_headers.append({})
                idx = len(_requested) - 1
                payload = _pages[idx] if idx < len(_pages) else {"search": {"total_rec": 0}, "results": []}
                return _Resp(json.dumps(payload), url)
            return _Resp("{}", url)

    connectors.urllib.request.build_opener = lambda *a, **k: _Op()
    connectors._polite_pause = lambda: None


def test_sends_mhrparams_header():
    # The list call MUST carry the mhrParams header or webitrent returns HTML.
    _install([{"search": {"total_rec": 1}, "results": [_role("1", "X")]}])
    connectors.webitrent({"url": LAUNCH})
    # urllib title-cases header keys, so look case-insensitively.
    hdrs = _req_headers[0] if _req_headers else {}
    mhr = next((v for k, v in hdrs.items() if k.lower() == "mhrparams"), "")
    check("mhrParams header present", bool(mhr))
    check("mhrParams carries WVID", "WVID=7102672HeH" in mhr)
    check("mhrParams carries USESSION", "USESSION=" in mhr)
    check("mhrParams carries empty search fields",
          "JOB_TITLE=" in mhr and "ORDER_BY=VACANCY_D" in mhr)


def test_parses_real_shape():
    _install([{"search": {"total_rec": 1}, "results": [_role("1238574aBi", "MEP Technical Project Lead")]}])
    jobs = connectors.webitrent({"url": LAUNCH})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from vacancy_id", j["id"] == "1238574aBi")
    check("title from job_title", j["title"] == "MEP Technical Project Lead")
    check("location from location_id", j["location"] == "The National Gallery")
    check("department blank (feed has none)", j["department"] == "")
    check("advert url built with WVID + VACANCY_ID",
          j["url"].endswith("ETREC179GF.open?WVID=7102672HeH&VACANCY_ID=1238574aBi"))


def test_html_entities_unescaped():
    _install([{"search": {"total_rec": 1}, "results": [_role("9", "Hospitality &amp; Events Manager")]}])
    jobs = connectors.webitrent({"url": LAUNCH})
    check("title entity unescaped", jobs[0]["title"] == "Hospitality & Events Manager")


def test_pages_to_total():
    p1 = {"search": {"total_rec": 5, "results_pp": "3"},
          "results": [_role(str(i), f"R{i}") for i in range(3)]}
    p2 = {"search": {"total_rec": 5, "results_pp": "3"},
          "results": [_role(str(10 + i), f"R{10+i}") for i in range(2)]}
    _install([p1, p2])
    jobs = connectors.webitrent({"url": LAUNCH})
    check("collected 5 across 2 pages", len(jobs) == 5)
    check("two list requests made", len(_requested) == 2)
    check("second page used REC_FROM",
          any("REC_FROM=4" in u for u in _requested))


def test_stops_on_empty_page():
    _install([{"search": {"total_rec": 99}, "results": []}])
    jobs = connectors.webitrent({"url": LAUNCH})
    check("empty results -> no jobs, no infinite loop", jobs == [])


def test_dedupes_by_id():
    merged = connectors._merge_by_id([
        {"id": "same", "title": "X", "location": "The National Gallery", "department": "", "url": ""},
        {"id": "same", "title": "X", "location": "Trafalgar Square", "department": "", "url": ""},
    ])
    check("dup id merged to one", len(merged) == 1)
    check("locations merged", merged[0]["location"] == "The National Gallery; Trafalgar Square")


def test_missing_session_raises():
    # Launch page with no USESSION -> connector raises a clear ConnectorError.
    _install([{"search": {"total_rec": 1}, "results": [_role("1", "X")]}],
             launch_html="<html>no token here</html>")
    try:
        connectors.webitrent({"url": LAUNCH})
        check("missing session should raise", False)
    except connectors.ConnectorError:
        check("missing session raises ConnectorError", True)


def test_requires_launch_url():
    try:
        connectors.webitrent({})
        check("missing url should raise", False)
    except connectors.ConnectorError:
        check("missing launch url raises ConnectorError", True)


def test_derives_host_prefix_wvid_from_url():
    # A full launch URL with extra params still yields a correct list call.
    _install([{"search": {"total_rec": 1}, "results": [_role("1", "X")]}])
    url = LAUNCH + "&USESSION=stale&LANG=USA"
    jobs = connectors.webitrent({"url": url})
    check("parsed despite extra params in launch url", len(jobs) == 1)
    check("list call hit the right tenant path",
          any("ce0838li_webrecruitment/wrd/run/etrec106gf.json" in u for u in _requested))


def test_detect_recognises_webitrent():
    r = detect.detect(LAUNCH)
    check("detect provider webitrent", r["provider"] == "webitrent")
    check("detect tier 1 now runnable", r["tier"] == 1)
    check("detect pulled WVID", r["config"].get("wvid") == "7102672HeH")
    check("detect kept full url", r["config"].get("url") == LAUNCH)


def test_webitrent_in_registry_requires_url():
    check("webitrent registered", "webitrent" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["webitrent"]
    check("webitrent requires url config", required == ["url"])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
