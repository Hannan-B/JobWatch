"""
test_deliveroo.py — Deliveroo connector + detect wiring (mocked)
================================================================
Verifies the Deliveroo connector parses the real WordPress REST shape, resolves
location/team names from the embedded wp:term block (not the bare taxonomy IDs),
HTML-unescapes titles, pages until a short page, dedupes, and that detect wires
it up. Deliveroo is NOT source-side scopable (no clean per-city query captured),
so there's no market_scope passthrough to test. Sandbox can't reach Deliveroo,
so HTTP is mocked.

Run:  python3 test_deliveroo.py
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


def _role(jid, title="Account Manager", location="London", team="Commercial",
          url=None):
    """Build one role in Deliveroo's real WP-REST shape (with _embed=wp:term)."""
    post = {
        "id": jid,
        "title": {"rendered": title},
        "link": url or f"https://careers.deliveroo.co.uk/role/role-{jid}/",
        "locations": [412],   # bare taxonomy IDs (what the raw feed carries)
        "teams": [404],
        "_embedded": {"wp:term": []},
    }
    groups = []
    if location is not None:
        groups.append([{"taxonomy": "location", "name": location}])
    if team is not None:
        groups.append([{"taxonomy": "team", "name": team}])
    post["_embedded"]["wp:term"] = groups
    return post


_pages_by_page = {}     # page number -> list of post dicts
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
        return json.dumps(_pages_by_page.get(page, []))
    connectors.http_get = fake_http_get
    connectors._polite_pause = lambda: None


def test_parses_real_shape():
    _install_http({1: [_role(279441)]})
    jobs = connectors.deliveroo({})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from post id", j["id"] == "279441")
    check("title parsed", j["title"] == "Account Manager")
    check("location from embedded term (not ID)", j["location"] == "London")
    check("department from team term", j["department"] == "Commercial")
    check("url from link", j["url"].endswith("/role/role-279441/"))


def test_html_entities_unescaped():
    _install_http({1: [_role(1, title="Account Manager &#8211; Dutch Speaking")]})
    jobs = connectors.deliveroo({})
    check("title HTML-unescaped", jobs[0]["title"] == "Account Manager – Dutch Speaking")
    check("no raw entity remains", "&#" not in jobs[0]["title"])


def test_location_blank_when_no_terms():
    _install_http({1: [_role(2, location=None)]})
    jobs = connectors.deliveroo({})
    check("location blank when no location term", jobs[0]["location"] == "")


def test_department_blank_when_no_team():
    _install_http({1: [_role(3, team=None)]})
    jobs = connectors.deliveroo({})
    check("department blank when no team term", jobs[0]["department"] == "")


def test_pages_until_short():
    # page 1 full (100), page 2 short (3) -> stop after page 2, total 103.
    p1 = [_role(i) for i in range(100)]
    p2 = [_role(1000 + i) for i in range(3)]
    _install_http({1: p1, 2: p2})
    jobs = connectors.deliveroo({})
    check("collected 103 across 2 pages", len(jobs) == 103)
    check("requested page 1 and 2", any("page=1" in u for u in _requested_urls)
          and any("page=2" in u for u in _requested_urls))


def test_requests_embed():
    _install_http({1: [_role(1)]})
    connectors.deliveroo({})
    u = _requested_urls[0]
    check("_embed=wp:term requested", "_embed=wp" in u and "term" in u)


def test_dedupes_multi_location():
    merged = connectors._merge_by_id([
        {"id": "same", "title": "X", "location": "London", "department": "", "url": ""},
        {"id": "same", "title": "X", "location": "Manchester", "department": "", "url": ""},
    ])
    check("multi-location role merged to one", len(merged) == 1)
    check("locations merged", merged[0]["location"] == "London; Manchester")


def test_detect_recognises_deliveroo():
    for url in ["https://careers.deliveroo.co.uk/join-the-team/",
                "https://careers.deliveroo.co.uk/wp-json/wp/v2/roles?page=1"]:
        r = detect.detect(url)
        check(f"detect deliveroo: {url[:45]}", r["provider"] == "deliveroo")


def test_deliveroo_in_connectors_registry():
    check("deliveroo registered", "deliveroo" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["deliveroo"]
    check("deliveroo needs no required config", required == [])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
