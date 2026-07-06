"""
test_spotify.py — Spotify Careers (lifeatspotify.com) connector (mocked)
=======================================================================
Spotify's careers site is WordPress; the real jobs feed (confirmed live
2026-06-30) is:
    GET https://api.lifeatspotify.com/wp-json/animal/v1/job/search?l=<slug>
returning {"result":[...], "main_categories":[...]} where each role has
{id (slug), text (title, may carry &amp;), main_category{name}, locations[]{location}}.

These tests verify: the connector hits the api.lifeatspotify.com endpoint, the
recursive extractor finds result[], the parser reads text->title (entities
unescaped), main_category.name->department, locations[].location (merged for
multi-site roles), builds lifeatspotify.com/jobs/<id> URLs, dedupes, and that
the candidate-probe + clear-error behaviour still holds when nothing serves.
Sandbox can't reach Spotify, so HTTP is mocked.

Run:  python3 test_spotify.py
"""

import json
from jobwatch import connectors

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


API_HIT = "api.lifeatspotify.com/wp-json/animal/v1/job/search"

# A faithful slice of the real response shape.
REAL_BODY = {
    "main_categories": [
        {"name": "Engineering", "slug": "engineering"},
        {"name": "Marketing", "slug": "marketing"},
    ],
    "result": [
        {"id": "senior-machine-learning-engineer-ads-rd",
         "text": "Senior Machine Learning Engineer, Ads R&amp;D",
         "main_category": {"name": "Engineering", "slug": "engineering"},
         "sub_category": {"name": "Machine Learning", "slug": "machine-learning"},
         "locations": [{"location": "New York", "slug": "new-york", "num_jobs": 44}],
         "job_type": {"name": "Permanent", "slug": "permanent"}},
        {"id": "cloud-security-engineer",
         "text": "Cloud Security Engineer",
         "main_category": {"name": "Engineering", "slug": "engineering"},
         "sub_category": {"name": "Security", "slug": "security"},
         "locations": [{"location": "London", "slug": "london", "num_jobs": 37},
                       {"location": "Stockholm", "slug": "stockholm", "num_jobs": 33}],
         "job_type": {"name": "Permanent", "slug": "permanent"}},
        {"id": "head-of-backstage-marketing-3",
         "text": "Head of Backstage Marketing",
         "main_category": {"name": "Marketing", "slug": "marketing"},
         "sub_category": {"name": "", "slug": False},
         "locations": [{"location": "London", "slug": "london", "num_jobs": 37}],
         "job_type": {"name": "", "slug": ""}},
    ],
    "time": 1782788221,
}

_PAGE = '<html><script>{"buildId":"v9FYpJnlkhdSMk5p0OhNw"}</script></html>'


def _install(api_body, fail_others=True):
    """Mock http_get/http_post_json: api endpoint serves api_body; the /jobs page
    serves a buildId; everything else fails (like the live www. candidates)."""
    requested = []
    def fake_get(url, headers=None):
        requested.append(("GET", url))
        if API_HIT in url:
            if api_body is None:
                raise Exception("simulated api failure")
            return json.dumps(api_body)
        if url.endswith("/jobs"):
            return _PAGE
        if fail_others:
            raise Exception("404 " + url)
        return "{}"
    def fake_post(url, payload, headers=None):
        requested.append(("POST", url))
        raise Exception("404 " + url)
    connectors.http_get = fake_get
    connectors.http_post_json = fake_post
    connectors._polite_pause = lambda: None
    return requested


def test_hits_api_endpoint_first():
    req = _install(REAL_BODY)
    connectors.fetch_jobs("spotify", {"location_list": ["London"]})
    # The api endpoint should be among the requests, and it should win (no www
    # api candidates needed after it succeeds).
    hit = [u for (m, u) in req if API_HIT in u]
    check("api.lifeatspotify endpoint was called", len(hit) >= 1)
    check("location slug passed as ?l=london", any("l=london" in u for u in hit))


def test_parses_real_shape():
    _install(REAL_BODY)
    jobs = connectors.fetch_jobs("spotify", {"location_list": ["London"]})
    check("all three roles parsed", len(jobs) == 3)
    by_id = {j["id"]: j for j in jobs}
    j = by_id.get("cloud-security-engineer")
    check("id is the slug", j is not None)
    check("title from text", j["title"] == "Cloud Security Engineer")
    check("multi-location merged", j["location"] == "London; Stockholm")
    check("department from main_category", j["department"] == "Engineering")
    check("url is jobs/<id>", j["url"] == "https://www.lifeatspotify.com/jobs/cloud-security-engineer")


def test_entities_unescaped():
    _install(REAL_BODY)
    jobs = connectors.fetch_jobs("spotify", {"location_list": ["London"]})
    ads = next((j for j in jobs if j["id"] == "senior-machine-learning-engineer-ads-rd"), None)
    check("ampersand entity unescaped in title", ads["title"] == "Senior Machine Learning Engineer, Ads R&D")


def test_blank_job_type_ok():
    _install(REAL_BODY)
    jobs = connectors.fetch_jobs("spotify", {"location_list": ["London"]})
    hb = next((j for j in jobs if j["id"] == "head-of-backstage-marketing-3"), None)
    check("role with blank job_type still parsed", hb is not None)
    check("its department still read", hb["department"] == "Marketing")


def test_no_location_fetches_broad():
    req = _install(REAL_BODY)
    jobs = connectors.fetch_jobs("spotify", {})
    check("no-location run still returns roles", len(jobs) == 3)
    # With no slug, the api call carries no ?l=
    hit = [u for (m, u) in req if API_HIT in u]
    check("api called without a location query", any("?l=" not in u for u in hit))


def test_dedupes_by_id():
    # The recursive extractor de-dupes raw rows by id (keeps first occurrence),
    # so a feed that repeats a role id collapses to one job.
    body = {"result": [
        {"id": "dup", "text": "A", "main_category": {"name": "Eng"},
         "locations": [{"location": "London"}]},
        {"id": "dup", "text": "A", "main_category": {"name": "Eng"},
         "locations": [{"location": "Paris"}]},
        {"id": "other", "text": "B", "main_category": {"name": "Eng"},
         "locations": [{"location": "London"}]},
    ]}
    _install(body)
    jobs = connectors.fetch_jobs("spotify", {"location_list": ["London"]})
    check("duplicate id collapsed to one", len(jobs) == 2)
    check("distinct ids both kept", {j["id"] for j in jobs} == {"dup", "other"})


def test_multi_location_in_one_role_merged():
    # A single role listing several locations[] becomes one "A; B" job.
    body = {"result": [
        {"id": "multi", "text": "Role", "main_category": {"name": "Eng"},
         "locations": [{"location": "London"}, {"location": "Stockholm"},
                       {"location": "New York"}]},
    ]}
    _install(body)
    jobs = connectors.fetch_jobs("spotify", {"location_list": ["London"]})
    check("one merged job", len(jobs) == 1)
    check("all locations joined", jobs[0]["location"] == "London; Stockholm; New York")


def test_clear_error_when_nothing_serves():
    # api fails AND all www candidates fail -> the clear ConnectorError.
    _install(None, fail_others=True)
    try:
        connectors.fetch_jobs("spotify", {"location_list": ["London"]})
        check("should raise when no endpoint serves", False)
    except connectors.ConnectorError as e:
        msg = str(e)
        check("raises ConnectorError", True)
        check("error names what it tried", "Tried:" in msg or "tried" in msg.lower())
        check("error mentions the api endpoint", "api.lifeatspotify.com" in msg)


def test_spotify_in_registry():
    check("spotify registered", "spotify" in connectors.CONNECTORS)


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
