"""
test_revolutpeople.py — Revolut People connector + detect wiring (mocked)
=========================================================================
Revolut People (revolutpeople.com) is a PLATFORM connector: the tenant slug
comes from the pasted URL, so one connector serves every employer on it.
Confirmed live 2026-07-25 against Cleo — a public JSON API, GET, no auth and no
cookie (a cookieless request returns 200 despite Cloudflare fronting the site).

The shape traps this pins, all seen in the real payload:
  * `function` is NULL on real roles, so a blind .get("name") raises,
  * `locations` is a LIST and roles routinely carry several,
  * a page past the end answers {"detail": "Invalid page."} as an HTTP error,
    so the crawl must be bounded by pages.total and never probe past it,
  * the payload states `count`, which the completeness guard checks.

Run:  python3 -m tests.test_revolutpeople
"""

import json

from jobwatch import connectors, detect, market_scope

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


URL = "https://revolutpeople.com/cleo/public/careers"
API = "https://revolutpeople.com/api/cleo/external/v3/postings"


def _loc(name, type_="office", country=None):
    return {"name": name, "type": type_, "country": {"name": country or name}}


def _posting(jid, title="Lead MLOps Engineer", locations=None, function="Engineering"):
    """One posting in Revolut People's real shape. function=None reproduces the
    live null the parser must survive."""
    return {
        "id": jid,
        "title": title,
        "locations": locations if locations is not None else [_loc("United Kingdom")],
        "function": {"name": function} if function is not None else None,
        "is_featured": False,
    }


def _payload(postings, count=None, total_pages=1, page_size=100):
    return {"pages": {"next": None, "previous": None,
                      "total": total_pages, "page_size": page_size},
            "count": len(postings) if count is None else count,
            "results": postings}


_requested = []


def _install(pages_by_number, counts=None):
    """pages_by_number: {1: [posting, ...], 2: [...]}. A page not present raises,
    the way the live API's 'Invalid page.' does."""
    global _requested
    _requested = []
    total_pages = max(pages_by_number) if pages_by_number else 1
    total_count = counts if counts is not None else sum(
        len(v) for v in pages_by_number.values())

    def fake_get(url, headers=None):
        _requested.append(url)
        import urllib.parse as up
        n = int(up.parse_qs(up.urlparse(url).query).get("page", ["1"])[0])
        if n not in pages_by_number:
            raise connectors.ConnectorError("Nothing found at that address (404).")
        return json.dumps(_payload(pages_by_number[n], count=total_count,
                                   total_pages=total_pages))

    connectors.http_get = fake_get
    connectors._polite_pause = lambda: None


# --- shape ----------------------------------------------------------------

def test_parses_real_shape():
    _install({1: [_posting("47e9edf3-1b71-4d13-8e1c-ef080d2925b8")]})
    jobs = connectors.revolutpeople({"url": URL})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id is the uuid", j["id"] == "47e9edf3-1b71-4d13-8e1c-ef080d2925b8")
    check("title parsed", j["title"] == "Lead MLOps Engineer")
    check("department from function.name", j["department"] == "Engineering")
    check("location from locations[].name", j["location"] == "United Kingdom")
    check("url is /position/<slug>-<uuid>",
          j["url"] == f"{URL}/position/lead-mlops-engineer-"
                      "47e9edf3-1b71-4d13-8e1c-ef080d2925b8")


def test_job_url_matches_a_confirmed_live_link():
    # Pinned against a real Cleo link supplied by the owner, 2026-07-25.
    _install({1: [_posting("38eaea4b-0e4a-477c-9f7b-c54b69d6b720",
                           title="Senior / Lead Data Scientist, Product Analytics")]})
    jobs = connectors.revolutpeople({"url": URL})
    check("exact live URL reproduced", jobs[0]["url"] ==
          "https://revolutpeople.com/cleo/public/careers/position/"
          "senior-lead-data-scientist-product-analytics-"
          "38eaea4b-0e4a-477c-9f7b-c54b69d6b720")


def test_slug_folds_punctuation():
    _install({1: [_posting("x", title="Staff Engineer - Ruby | UK & EU (remote)"),
                  _posting("y", title="(Senior) Machine Learning Engineer, Chat")]})
    jobs = connectors.revolutpeople({"url": URL})
    check("pipes, ampersands and brackets fold to hyphens",
          jobs[0]["url"].endswith("staff-engineer-ruby-uk-eu-remote-x"))
    check("leading bracket stripped, no double hyphens",
          jobs[1]["url"].endswith("senior-machine-learning-engineer-chat-y"))


def test_null_function_does_not_crash():
    # THE trap: 'function' is null on real Cleo roles.
    _install({1: [_posting("a", title="Product Design Manager", function=None),
                  _posting("b", title="Director of InfoSec | UK", function=None),
                  _posting("c", function="Design")]})
    jobs = connectors.revolutpeople({"url": URL})
    check("null function survives", len(jobs) == 3)
    check("null function -> blank department",
          [j["department"] for j in jobs] == ["", "", "Design"])


def test_multi_location_joined():
    _install({1: [_posting("m", locations=[
        _loc("Poland", "remote"), _loc("Portugal", "remote"),
        _loc("Spain", "remote"), _loc("UK - Remote", "remote", "United Kingdom"),
        _loc("United Kingdom")])]})
    jobs = connectors.revolutpeople({"url": URL})
    check("all locations joined in order",
          jobs[0]["location"] == "Poland; Portugal; Spain; UK - Remote; United Kingdom")


def test_duplicate_location_names_deduped():
    _install({1: [_posting("d", locations=[
        _loc("United Kingdom"), _loc("United Kingdom", "remote"),
        _loc("Spain", "remote")])]})
    jobs = connectors.revolutpeople({"url": URL})
    check("repeated name kept once", jobs[0]["location"] == "United Kingdom; Spain")


def test_empty_locations_gives_blank():
    _install({1: [_posting("e", locations=[])]})
    jobs = connectors.revolutpeople({"url": URL})
    check("no locations -> blank location", jobs[0]["location"] == "")


def test_falls_back_to_country_when_name_missing():
    _install({1: [_posting("f", locations=[
        {"name": "", "type": "remote", "country": {"name": "Portugal"}}])]})
    jobs = connectors.revolutpeople({"url": URL})
    check("country used when name blank", jobs[0]["location"] == "Portugal")


def test_posting_without_title_skipped():
    _install({1: [{"id": "x", "title": "", "locations": [], "function": None},
                  _posting("y")]})
    jobs = connectors.revolutpeople({"url": URL})
    check("untitled posting skipped", len(jobs) == 1)
    check("kept the titled one", jobs[0]["id"] == "y")


# --- tenant / URL ---------------------------------------------------------

def test_derives_tenant_and_api_url():
    _install({1: [_posting("1")]})
    connectors.revolutpeople({"url": URL})
    check("hits the tenant's API", _requested[0].startswith(API))
    check("asks for page 1", "page=1" in _requested[0])


def test_tracking_params_do_not_leak_into_the_api_call():
    _install({1: [_posting("1")]})
    connectors.revolutpeople(
        {"url": URL + "?pid=website&af_sub4=862a1a20-fbec-4f08-b1e1-dce2fb876c01&locations=1"})
    check("no tracking id sent", "af_sub4" not in _requested[0])
    check("no pid sent", "pid=" not in _requested[0])
    check("no location filter pinned", "locations=" not in _requested[0])


def test_works_for_another_tenant():
    _install({1: [_posting("1")]})
    connectors.revolutpeople({"url": "https://revolutpeople.com/othercorp/public/careers"})
    check("tenant taken from the URL",
          _requested[0].startswith("https://revolutpeople.com/api/othercorp/external/v3/postings"))


def test_requires_url():
    try:
        connectors.revolutpeople({})
        check("missing url should raise", False)
    except connectors.ConnectorError:
        check("missing url raises ConnectorError", True)


def test_url_without_tenant_raises_clearly():
    try:
        connectors.revolutpeople({"url": "https://revolutpeople.com/"})
        check("tenant-less url should raise", False)
    except connectors.ConnectorError as e:
        check("tenant-less url raises ConnectorError", True)
        check("error shows the expected shape", "<company>" in str(e))


# --- paging ---------------------------------------------------------------

def test_single_page_makes_one_request():
    _install({1: [_posting(str(i)) for i in range(37)]})
    jobs = connectors.revolutpeople({"url": URL})
    check("all 37 collected", len(jobs) == 37)
    check("exactly one request", len(_requested) == 1)


def test_never_probes_past_the_last_page():
    # Page 2 raises (the live 'Invalid page.'), so requesting it at all is a bug.
    _install({1: [_posting(str(i)) for i in range(5)]})
    connectors.revolutpeople({"url": URL})
    check("did not request page 2", not any("page=2" in u for u in _requested))


def test_follows_pages_total():
    _install({1: [_posting(f"a{i}") for i in range(100)],
              2: [_posting(f"b{i}") for i in range(20)]})
    jobs = connectors.revolutpeople({"url": URL})
    check("both pages collected", len(jobs) == 120)
    check("requested page 2", any("page=2" in u for u in _requested))
    check("stopped at pages.total", not any("page=3" in u for u in _requested))


def test_dedupes_across_pages():
    shared = _posting("dup")
    _install({1: [shared, _posting("a")], 2: [shared, _posting("b")]},
             counts=3)
    jobs = connectors.revolutpeople({"url": URL})
    check("repeated id collapsed", len(jobs) == 3)


# --- guard ----------------------------------------------------------------

def test_guard_raises_when_materially_short():
    _install({1: [_posting(str(i)) for i in range(5)]}, counts=500)
    try:
        connectors.revolutpeople({"url": URL})
        check("short fetch should raise", False)
    except connectors.ConnectorError as e:
        msg = str(e)
        check("short fetch raises ConnectorError", True)
        check("error names the reported count", "500" in msg)
        check("error says incomplete", "incomplete" in msg.lower())


def test_guard_tolerates_small_drift():
    _install({1: [_posting(str(i)) for i in range(98)]}, counts=100)
    jobs = connectors.revolutpeople({"url": URL})
    check("2-role shortfall tolerated", len(jobs) == 98)


def test_no_results_raises():
    _install({1: []}, counts=0)
    try:
        connectors.revolutpeople({"url": URL})
        check("empty board should raise", False)
    except connectors.ConnectorError as e:
        check("empty board raises ConnectorError", True)
        check("error names the tenant", "cleo" in str(e))


# --- wiring ---------------------------------------------------------------

def test_detect_recognises_revolutpeople():
    for u in [URL, URL + "?pid=website&locations=1"]:
        r = detect.detect(u)
        check(f"detect revolutpeople: {u[-30:]}", r["provider"] == "revolutpeople")
        check("tier 1 runnable", r["tier"] == 1)
        check("suggested key is the tenant", r["suggested_key"] == "cleo")
    check("detect keeps the full url", detect.detect(URL)["config"].get("url") == URL)


def test_registry_requires_url():
    check("revolutpeople registered", "revolutpeople" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["revolutpeople"]
    check("requires url config", required == ["url"])


def test_not_source_scopable():
    check("not in SCOPABLE", "revolutpeople" not in market_scope.SCOPABLE)
    cfg = market_scope.scoped_config("revolutpeople", {"url": URL}, ["London"])
    check("scoped_config leaves config untouched", cfg == {"url": URL})


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
