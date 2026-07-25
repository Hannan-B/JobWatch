"""
test_avature.py — Avature career portals connector + detect wiring (mocked)
===========================================================================
Avature (*.avature.net) is a PLATFORM connector: tenant subdomain and portal
path come from the pasted URL, so one connector serves every Avature employer.

The load-bearing behaviour here is PAGING. Measured live on bloomberg.avature.net
(2026-07-25): jobRecordsPerPage is ignored (always 12), sortBy is ignored, and
the result order DRIFTS between requests — adjacent pages re-serve roles and the
pages aren't ordered by id. A straight 12-step crawl collected 406 of 426, with
the missing set varying per run. For JobWatch that's worse than missing data:
compare would report those roles removed, then added again next check.

So the connector HALF-STEPS (windows overlap by half a page, absorbing drift;
_merge_by_id collapses the repeats) and carries a COMPLETENESS GUARD (Avature
prints its own total in aria-label="426 results"; a material shortfall raises
rather than silently returning a short list).

These tests lock: the real row shape, base-URL derivation across pasted forms,
the deliberate dropping of a pasted location facet, half-step offsets, dedupe of
overlapping windows, drift recovery, and both directions of the guard.

Run:  python3 -m tests.test_avature
"""

from jobwatch import connectors, detect, market_scope

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


URL = "https://bloomberg.avature.net/careers/SearchJobs"
HOST = "bloomberg.avature.net"


def _article(jid, title="Senior Software Engineer", location="London, United Kingdom"):
    """One role in Avature's real portal markup."""
    slug = title.replace(" ", "-").replace(",", "")
    return (
        '<article class="article article--result" id="article--%s">'
        '<div class="article__header"><div class="article__header__text">'
        '<h3 class="article__header__text__title title title--04">'
        f'<a class="link" href="https://{HOST}/careers/JobDetail/{slug}/{jid}"> {title} </a>'
        '</h3><div class="article__header__text__subtitle">'
        f'<span class="list-item-location">{location}</span>'
        '</div></div></div>'
        '<div class="article__footer">'
        f'<a class="button button--primary" href="https://{HOST}/careers/JobDetail/{slug}/{jid}"> Apply </a>'
        f'<a class="button button--secondary" href="https://{HOST}/careers/SaveJob?jobId={jid}"> Save </a>'
        '</div></article>' % jid)


def _page(articles, total=None):
    legend = ""
    if total is not None:
        legend = ('<div class="list-controls__text__legend" '
                  f'aria-label="{total} results"> 1-12 of {total} results </div>')
    return (f'<html><body><div class="list-controls">{legend}</div>'
            '<div class="results results--listed">' + "".join(articles) +
            '</div></body></html>')


_requested = []


def _install(ids, page_size=12, total=None, drift=0, roles=None):
    """Serve a window of `ids` for each jobOffset.

    drift>0 simulates Avature's reshuffling: each request nudges the window
    forward by a varying amount, so a naive full-step crawl skips roles that
    slid backwards out of one window before the next one started.
    """
    global _requested
    _requested = []
    state = {"n": 0}
    reported = len(ids) if total is None else total

    def fake_get(url, headers=None):
        _requested.append(url)
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(url).query)
        off = int(q.get("jobOffset", ["0"])[0])
        d = 0
        if drift:
            d = (state["n"] * 7) % (drift + 1)     # deterministic, varying
        state["n"] += 1
        window = ids[off + d: off + d + page_size]
        arts = [(roles or {}).get(i) or _article(i) for i in window]
        return _page(arts, total=reported)

    connectors.http_get = fake_get
    connectors._polite_pause = lambda: None


def _offsets():
    import urllib.parse as up
    out = []
    for u in _requested:
        q = up.parse_qs(up.urlparse(u).query)
        out.append(int(q.get("jobOffset", ["0"])[0]))
    return out


# --- shape ----------------------------------------------------------------

def test_parses_real_shape():
    _install(["21018"], page_size=12, total=1)
    jobs = connectors.avature({"url": URL})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from /JobDetail/<slug>/<id>", j["id"] == "21018")
    check("title parsed and trimmed", j["title"] == "Senior Software Engineer")
    check("location from list-item-location", j["location"] == "London, United Kingdom")
    check("department blank (not in list view)", j["department"] == "")
    check("url is the absolute JobDetail link",
          j["url"].startswith(f"https://{HOST}/careers/JobDetail/") and j["url"].endswith("/21018"))


def test_html_entities_unescaped():
    art = _article("1", title="Research &amp; Development Lead")
    _install(["1"], total=1, roles={"1": art})
    jobs = connectors.avature({"url": URL})
    check("title entity unescaped", jobs[0]["title"] == "Research & Development Lead")


def test_distinct_roles_sharing_a_title_are_kept():
    # Bloomberg really does post two roles with identical titles (21038/21039);
    # dedupe must key on ID, never on title.
    _install(["21038", "21039"], total=2)
    jobs = connectors.avature({"url": URL})
    check("both same-titled roles kept", len(jobs) == 2)
    check("ids distinct", {j["id"] for j in jobs} == {"21038", "21039"})


# --- base URL derivation (platform behaviour) -----------------------------

def test_derives_base_from_various_pasted_urls():
    for pasted in [
        "https://bloomberg.avature.net/careers/SearchJobs",
        "https://bloomberg.avature.net/careers/SearchJobs/",
        "https://bloomberg.avature.net/careers",
        "https://bloomberg.avature.net/careers/JobDetail/Some-Role/21018",
    ]:
        _install(["1"], total=1)
        connectors.avature({"url": pasted})
        check(f"base derived from {pasted[-28:]:28}",
              _requested[0] == f"https://{HOST}/careers/SearchJobs")


def test_works_for_a_different_tenant_and_portal():
    _install(["1"], total=1)
    connectors.avature({"url": "https://othercorp.avature.net/jobs/SearchJobs?x=1"})
    check("tenant + portal path both taken from the URL",
          _requested[0] == "https://othercorp.avature.net/jobs/SearchJobs")


def test_pasted_location_facet_is_dropped():
    # THE DELIBERATE CHOICE: a pasted London facet must NOT pin the fetch, or a
    # later change to the chosen cities would silently drop roles.
    _install(["1"], total=1)
    connectors.avature({"url": URL + "/?1845=%5B162558%5D&1845_format=3996&listFilterMode=1"})
    check("facet query dropped from the fetch", "1845" not in _requested[0])
    check("fetched the unfiltered board",
          _requested[0] == f"https://{HOST}/careers/SearchJobs")


def test_requires_url():
    try:
        connectors.avature({})
        check("missing url should raise", False)
    except connectors.ConnectorError:
        check("missing url raises ConnectorError", True)


# --- paging: the hard part ------------------------------------------------

def test_pages_by_HALF_steps():
    ids = [str(1000 + i) for i in range(48)]
    _install(ids, page_size=12, total=48)
    connectors.avature({"url": URL})
    offs = _offsets()
    check("first request has no offset", offs[0] == 0)
    check("steps by 6, not 12", offs[:5] == [0, 6, 12, 18, 24])
    check("did not step by a full page", 12 not in offs[1:2])


def test_overlapping_windows_are_deduped():
    ids = [str(2000 + i) for i in range(36)]
    _install(ids, page_size=12, total=36)
    jobs = connectors.avature({"url": URL})
    check("36 roles collected exactly once each", len(jobs) == 36)
    check("no duplicate ids", len({j["id"] for j in jobs}) == 36)


def test_half_step_recovers_roles_that_drift():
    # The real failure: with drift, a full-step crawl loses roles. Half-stepping
    # must still collect every one.
    ids = [str(3000 + i) for i in range(60)]
    _install(ids, page_size=12, total=60, drift=3)
    jobs = connectors.avature({"url": URL})
    check("every role collected despite drift", len(jobs) == 60)
    check("ids complete", {j["id"] for j in jobs} == set(ids))


def test_stops_and_does_not_hammer():
    ids = [str(4000 + i) for i in range(24)]
    _install(ids, page_size=12, total=24)
    connectors.avature({"url": URL})
    check("small board finishes in few requests", len(_requested) <= 6)
    check("never runs past the reported total", max(_offsets()) < 24)


# --- the completeness guard ----------------------------------------------

def test_guard_raises_when_materially_short():
    # Board claims 500 but only 24 roles exist to serve -> must refuse.
    ids = [str(5000 + i) for i in range(24)]
    _install(ids, page_size=12, total=500)
    try:
        connectors.avature({"url": URL})
        check("materially short fetch should raise", False)
    except connectors.ConnectorError as e:
        msg = str(e)
        check("short fetch raises ConnectorError", True)
        check("error names the reported total", "500" in msg)
        check("error names what was collected", "24" in msg)
        check("error explains it is incompleteness, not emptiness",
              "incomplete" in msg.lower())


def test_guard_tolerates_a_small_honest_drift():
    # The board's own total moved 436 -> 426 during live testing, so a couple of
    # roles disappearing mid-crawl must NOT fail the run.
    ids = [str(6000 + i) for i in range(118)]
    _install(ids, page_size=12, total=120)      # 2 short of 120
    jobs = connectors.avature({"url": URL})
    check("small shortfall tolerated", len(jobs) == 118)


def test_no_rows_raises_rather_than_reporting_zero():
    _install([], page_size=12, total=0)
    try:
        connectors.avature({"url": URL})
        check("empty board should raise", False)
    except connectors.ConnectorError as e:
        check("empty parse raises ConnectorError", True)
        check("error points at the markup", "article--result" in str(e))


def test_missing_total_still_returns_jobs():
    # A portal without the legend element: no guard possible, but the crawl must
    # still work and terminate.
    ids = [str(7000 + i) for i in range(24)]
    _install(ids, page_size=12, total=None)
    jobs = connectors.avature({"url": URL})
    check("jobs still collected with no total", len(jobs) == 24)


# --- wiring ---------------------------------------------------------------

def test_detect_recognises_avature():
    for u in [URL,
              "https://bloomberg.avature.net/careers/SearchJobs/?1845=%5B162558%5D",
              "https://othercorp.avature.net/jobs/SearchJobs"]:
        r = detect.detect(u)
        check(f"detect avature: {u[:46]}", r["provider"] == "avature")
        check(f"tier 1 runnable: {u[:46]}", r["tier"] == 1)
    r = detect.detect(URL)
    check("detect keeps the full url", r["config"].get("url") == URL)
    check("detect keeps the host", r["config"].get("host") == HOST)


def test_avature_in_registry_requires_url():
    check("avature registered", "avature" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["avature"]
    check("avature requires url config", required == ["url"])


def test_avature_is_not_source_scopable():
    # Avature's location facet is an opaque tenant-specific id, so it can never
    # be driven from a chosen city name — it must stay unscopable.
    check("avature not in SCOPABLE", "avature" not in market_scope.SCOPABLE)
    cfg = market_scope.scoped_config("avature", {"url": URL}, ["London"])
    check("scoped_config leaves avature config untouched",
          cfg == {"url": URL} and "location_list" not in cfg)


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
