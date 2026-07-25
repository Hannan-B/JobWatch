"""
test_successfactors.py — SAP SuccessFactors connector + detect wiring (mocked)
==============================================================================
SuccessFactors (Recruiting Marketing / Career Site Builder) is a PLATFORM
connector: host and SITE ID both come from the pasted URL, so one connector
serves every tenant.

Two things carry real risk here and are pinned hard below.

1. SITE SCOPING. Many SuccessFactors installs are shared by a whole GROUP, one
   site id per company. Penguin Random House UK sits on Bertelsmann's install:
   the bare /search/ returns 826 roles across RTL, Arvato, Riverty and others,
   while /PRH_UK/search/ returns Penguin's 19. Point the connector at the wrong
   one and you silently track the parent conglomerate.

2. DOUBLE MARKUP. Every <tr class="data-row"> renders the title link twice (a
   desktop .hidden-phone copy and a .visible-phone one) and the location twice.
   A naive whole-page regex yields two jobs per role.

Also covered: startrow paging with a PINNED sort, the completeness guard, and
its half-step retry (an unpinned/tied sort is what broke Avature's paging).

Verified against a real PRH_UK capture (19 roles, 2026-07-25).

Run:  python3 -m tests.test_successfactors
"""

from jobwatch import connectors, detect, market_scope

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


HOST = "jobsearch.createyourowncareer.com"
URL = f"https://{HOST}/PRH_UK/search/?q=&locale=en_GB"


def _row(jid, title="Marketing Executive, Ebury",
         location="London, United Kingdom, SW11 7BW", site="PRH_UK"):
    """One results row in SuccessFactors' real markup — INCLUDING the duplicate
    .visible-phone copy of the title link and location that the live page ships."""
    href = f"/{site}/job/London-{title.replace(' ', '-')}-SW11-7BW/{jid}/"
    return (
        '<tr class="data-row">'
        '<td class="colTitle" headers="hdrTitle">'
        f'<span class="jobTitle hidden-phone">'
        f'<a href="{href}" class="jobTitle-link">{title}</a></span>'
        '<div class="jobdetail-phone visible-phone">'
        f'<span class="jobTitle visible-phone">'
        f'<a class="jobTitle-link" href="{href}">{title}</a></span>'
        f'<span class="jobLocation visible-phone">'
        f'<span class="jobLocation"> {location} </span></span>'
        '<span class="jobDate visible-phone">24 Jul 2026 </span></div></td>'
        '<td class="colLocation hidden-phone" headers="hdrLocation">'
        f'<span class="jobLocation"> {location} </span></td>'
        '<td class="hidden-phone"></td></tr>')


def _page(rows, shown_from=1, shown_to=None, total=None):
    shown_to = shown_to if shown_to is not None else len(rows)
    label = ""
    if total is not None:
        label = ('<div class="pagination-label-row"><span class="paginationLabel" '
                 f'aria-label="Results {shown_from} – {shown_to}">Results '
                 f'<b>{shown_from} – {shown_to}</b> of <b>{total}</b></span></div>')
    return (f'<html><body>{label}<table><tbody>' + "".join(rows) +
            '</tbody></table></body></html>')


_requested = []


def _install(ids, page_size=25, total=None, drift=0):
    global _requested
    _requested = []
    state = {"n": 0}
    reported = len(ids) if total is None else total

    def fake_get(url, headers=None):
        _requested.append(url)
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(url).query)
        start = int(q.get("startrow", ["0"])[0])
        d = (state["n"] * 7) % (drift + 1) if drift else 0
        state["n"] += 1
        window = ids[start + d: start + d + page_size]
        return _page([_row(i) for i in window],
                     shown_from=start + 1, shown_to=start + len(window),
                     total=reported)

    connectors.http_get = fake_get
    connectors._polite_pause = lambda: None


# --- shape ----------------------------------------------------------------

def test_parses_real_shape():
    _install(["1418725833"], total=1)
    jobs = connectors.successfactors({"url": URL})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from /job/<slug>/<id>/", j["id"] == "1418725833")
    check("title parsed", j["title"] == "Marketing Executive, Ebury")
    check("location from jobLocation", j["location"] == "London, United Kingdom, SW11 7BW")
    check("department blank (not in results table)", j["department"] == "")
    check("relative href made absolute",
          j["url"] == f"https://{HOST}/PRH_UK/job/London-Marketing-Executive,-Ebury-SW11-7BW/1418725833/")


def test_double_markup_yields_ONE_job_per_row():
    # Each row ships the title link twice and the location twice. Three roles
    # must be three jobs, not six.
    _install(["1", "2", "3"], total=3)
    jobs = connectors.successfactors({"url": URL})
    check("3 rows -> 3 jobs (not 6)", len(jobs) == 3)
    check("ids unique and in order", [j["id"] for j in jobs] == ["1", "2", "3"])


def test_html_entities_unescaped():
    connectors.http_get = lambda u, headers=None: _page(
        [_row("9", title="Head of Licensing &amp; Partnerships (6m FTC)")], total=1)
    connectors._polite_pause = lambda: None
    jobs = connectors.successfactors({"url": URL})
    check("title entity unescaped",
          jobs[0]["title"] == "Head of Licensing & Partnerships (6m FTC)")


# --- site scoping: the thing that matters most ----------------------------

def test_scopes_to_the_site_id_in_the_url():
    _install(["1"], total=1)
    connectors.successfactors({"url": URL})
    check("fetched the PRH_UK site, not the group",
          _requested[0].startswith(f"https://{HOST}/PRH_UK/search/"))


def test_site_id_derived_from_any_page_on_the_site():
    for pasted in [f"https://{HOST}/PRH_UK/search/?q=&locale=en_GB",
                   f"https://{HOST}/PRH_UK/content/home/?locale=en_GB",
                   f"https://{HOST}/PRH_UK/job/London-Some-Role-SW11/1418725833/",
                   f"https://{HOST}/PRH_UK/"]:
        _install(["1"], total=1)
        connectors.successfactors({"url": pasted})
        check(f"site id from {pasted[-26:]:28}",
              _requested[0].startswith(f"https://{HOST}/PRH_UK/search/"))


def test_group_wide_url_stays_group_wide():
    # A URL with no site id is the shared group search. We honour it rather than
    # guessing a tenant — but the docstring warns what that means.
    _install(["1"], total=1)
    connectors.successfactors({"url": f"https://{HOST}/search/?q="})
    check("no site id -> bare /search/",
          _requested[0].startswith(f"https://{HOST}/search/?"))


def test_works_for_a_different_tenant_and_host():
    _install(["1"], total=1)
    connectors.successfactors({"url": "https://careers.example.com/ACME_UK/search/"})
    check("host and site id both taken from the URL",
          _requested[0].startswith("https://careers.example.com/ACME_UK/search/"))


def test_requires_url():
    try:
        connectors.successfactors({})
        check("missing url should raise", False)
    except connectors.ConnectorError:
        check("missing url raises ConnectorError", True)


# --- paging ---------------------------------------------------------------

def test_pins_the_sort():
    _install(["1"], total=1)
    connectors.successfactors({"url": URL})
    u = _requested[0]
    check("sortColumn pinned", "sortColumn=referencedate" in u)
    check("sortDirection pinned", "sortDirection=desc" in u)


def test_single_page_does_not_over_fetch():
    _install([str(i) for i in range(19)], page_size=25, total=19)
    jobs = connectors.successfactors({"url": URL})
    check("all 19 roles from one page", len(jobs) == 19)
    check("only one request made", len(_requested) == 1)


def test_pages_by_startrow():
    _install([str(i) for i in range(60)], page_size=25, total=60)
    jobs = connectors.successfactors({"url": URL})
    check("all 60 collected", len(jobs) == 60)
    starts = []
    import urllib.parse as up
    for u in _requested:
        starts.append(int(up.parse_qs(up.urlparse(u).query).get("startrow", ["0"])[0]))
    check("startrow steps by the page size", starts[:3] == [0, 25, 50])


def test_half_step_retry_recovers_from_drift():
    # Ties on the sort key can shuffle rows between requests. A full-step crawl
    # would come up short; the guard's retry must recover it rather than raise.
    _install([str(i) for i in range(80)], page_size=25, total=80, drift=4)
    jobs = connectors.successfactors({"url": URL})
    check("drifting board still fully collected", len(jobs) == 80)


# --- guard ----------------------------------------------------------------

def test_guard_raises_when_materially_short():
    _install([str(i) for i in range(20)], page_size=25, total=500)
    try:
        connectors.successfactors({"url": URL})
        check("short fetch should raise", False)
    except connectors.ConnectorError as e:
        msg = str(e)
        check("short fetch raises ConnectorError", True)
        check("error names the reported total", "500" in msg)
        check("error says incomplete", "incomplete" in msg.lower())


def test_guard_tolerates_small_honest_drift():
    _install([str(i) for i in range(198)], page_size=25, total=200)
    jobs = connectors.successfactors({"url": URL})
    check("2-role shortfall tolerated", len(jobs) == 198)


def test_no_rows_raises_rather_than_reporting_zero():
    connectors.http_get = lambda u, headers=None: _page([], total=0)
    connectors._polite_pause = lambda: None
    try:
        connectors.successfactors({"url": URL})
        check("empty board should raise", False)
    except connectors.ConnectorError as e:
        check("empty parse raises ConnectorError", True)
        check("error points at the markup", "data-row" in str(e))


def test_missing_total_still_returns_jobs():
    _install([str(i) for i in range(25)], page_size=25, total=None)
    connectors.http_get = lambda u, headers=None, _r=[_row(str(i)) for i in range(5)]: _page(_r)
    jobs = connectors.successfactors({"url": URL})
    check("jobs collected with no total label", len(jobs) == 5)


# --- wiring ---------------------------------------------------------------

def test_detect_recognises_successfactors():
    r = detect.detect(URL)
    check("detect provider successfactors", r["provider"] == "successfactors")
    check("detect tier 1 runnable", r["tier"] == 1)
    check("suggested key is the site id, not the shared host",
          r["suggested_key"] == "prh-uk")
    check("detect keeps the full url", r["config"].get("url") == URL)


def test_detect_native_successfactors_host():
    r = detect.detect("https://career5.successfactors.com/ACME/search/")
    check("*.successfactors.com recognised", r["provider"] == "successfactors")


def test_detect_unknown_custom_domain_not_claimed():
    # A custom domain not on the allow-list can't be told apart by URL alone.
    r = detect.detect("https://careers.someunknownco.com/ACME/search/")
    check("unknown custom domain not claimed", r["provider"] != "successfactors")


def test_registry_requires_url():
    check("successfactors registered", "successfactors" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["successfactors"]
    check("requires url config", required == ["url"])


def test_not_source_scopable():
    check("not in SCOPABLE", "successfactors" not in market_scope.SCOPABLE)
    cfg = market_scope.scoped_config("successfactors", {"url": URL}, ["London"])
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
