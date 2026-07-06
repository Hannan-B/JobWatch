"""
test_sohohouse.py — Soho House connector + detect wiring (mocked)
=================================================================
Verifies the Soho House connector reads the Next.js vacancy data: prefers the
inline __NEXT_DATA__ vacancies on the careers page, falls back to fetching
/_next/data/<buildId>/careers.json when inline isn't present, parses the real
shape (id / job_title / job_location city / department), builds /careers/<id>
URLs, unescapes entities, dedupes, and that detect recognises the careers host.
Soho House is global but NOT source-side scopable (static file). Sandbox can't
reach it, so HTTP is mocked.

Run:  python3 test_sohohouse.py
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


def _vac(jid, title, location="London", dept="Bar"):
    return {"id": jid, "job_title": title, "job_location": location,
            "department": dept, "venue": "Soho House", "region": "United Kingdom",
            "department_area": "Food & Beverage"}


def _page_inline(vacancies, build_id="KUnvoY4BkFeKjOwVLbZmh"):
    """A careers page whose __NEXT_DATA__ embeds the vacancies inline."""
    nd = {"buildId": build_id,
          "props": {"pageProps": {"vacancies": vacancies}},
          "page": "/careers"}
    return ('<html><body><div id="root"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
            '</body></html>')


def _page_no_inline(build_id="BUILD123"):
    """A careers page with a buildId but NO inline vacancies (forces fallback)."""
    nd = {"buildId": build_id, "props": {"pageProps": {}}, "page": "/careers"}
    return ('<html><body>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
            '</body></html>')


_requested = []
def _install(page_html, data_json=None):
    global _requested
    _requested = []
    def fake_get(url, headers=None):
        _requested.append(url)
        if "/_next/data/" in url:
            return data_json if data_json is not None else '{"pageProps":{"vacancies":[]}}'
        return page_html
    connectors.http_get = fake_get


def test_reads_inline_vacancies():
    _install(_page_inline([_vac(4905337101, "Senior Chef de Partie - White City House")]))
    jobs = connectors.sohohouse({})
    check("one job parsed inline", len(jobs) == 1)
    j = jobs[0]
    check("id from numeric id", j["id"] == "4905337101")
    check("title parsed", j["title"] == "Senior Chef de Partie - White City House")
    check("location is the city", j["location"] == "London")
    check("department parsed", j["department"] == "Bar")
    check("url built as /careers/<id>", j["url"] == "https://careers.sohohouse.com/careers/4905337101")
    check("did NOT need the data file", not any("/_next/data/" in u for u in _requested))


def test_falls_back_to_data_file():
    data = json.dumps({"pageProps": {"vacancies": [_vac(1, "Bartender"), _vac(2, "Runner")]}})
    _install(_page_no_inline(build_id="BUILD123"), data_json=data)
    jobs = connectors.sohohouse({})
    check("two jobs from data file", len(jobs) == 2)
    check("fetched the data file with the build id",
          any("/_next/data/BUILD123/careers.json" in u for u in _requested))


def test_html_entities_unescaped():
    _install(_page_inline([_vac(7, "Chef &amp; Manager")]))
    jobs = connectors.sohohouse({})
    check("title entity unescaped", jobs[0]["title"] == "Chef & Manager")


def test_blank_location_kept():
    # The evergreen "Are You Interested" entry has no location — kept, blank loc.
    _install(_page_inline([_vac(4228649101, "Are You Interested In A Career?", location="", dept="")]))
    jobs = connectors.sohohouse({})
    check("blank-location role kept", len(jobs) == 1)
    check("location blank", jobs[0]["location"] == "")
    check("department blank", jobs[0]["department"] == "")


def test_skips_entry_without_title():
    _install(_page_inline([
        {"id": 1, "job_title": "", "job_location": "London"},
        _vac(2, "Real Role"),
    ]))
    jobs = connectors.sohohouse({})
    check("entry without title skipped", len(jobs) == 1)
    check("kept the titled role", jobs[0]["id"] == "2")


def test_dedupes_by_id():
    merged = connectors._merge_by_id([
        {"id": "same", "title": "X", "location": "London", "department": "", "url": ""},
        {"id": "same", "title": "X", "location": "Paris", "department": "", "url": ""},
    ])
    check("dup id merged", len(merged) == 1)
    check("locations merged", merged[0]["location"] == "London; Paris")


def test_missing_build_id_raises():
    _install("<html><body>no next data here</body></html>")
    try:
        connectors.sohohouse({})
        check("missing build id should raise", False)
    except connectors.ConnectorError:
        check("missing build id raises ConnectorError", True)


def test_detect_recognises_sohohouse():
    for url in ["https://careers.sohohouse.com/careers",
                "https://careers.sohohouse.com/careers/4905337101?location=London"]:
        r = detect.detect(url)
        check(f"detect sohohouse: {url[:45]}", r["provider"] == "sohohouse")


def test_sohohouse_in_registry():
    check("sohohouse registered", "sohohouse" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["sohohouse"]
    check("sohohouse needs no required config", required == [])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
