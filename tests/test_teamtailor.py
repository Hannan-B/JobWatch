"""
test_teamtailor.py — Teamtailor connector + detect wiring (mocked)
==================================================================
Verifies the Teamtailor connector parses the server-rendered #jobs_list_container
markup: pulls the /jobs/<id> id from the title link, reads the title (entities
unescaped), and reads department + location STRUCTURALLY from the meta div — the
department span is ABSENT on roles without a department, so the parser must not
take "first span = department" positionally. It also makes URLs absolute on the
tenant host, pages via ?page=N until an empty page, dedupes, and detect
recognises a *.teamtailor.com URL (custom domains stay Tier 3 until the connector
is selected). Teamtailor is a PLATFORM connector (host from the pasted URL), like
webitrent/ciphr. Sandbox can't reach it, so HTTP is mocked.

Run:  python3 test_teamtailor.py
"""

from jobwatch import connectors, detect

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


LIST_URL = "https://careers.yotoplay.com/jobs"
HOST = "careers.yotoplay.com"

# The remote-status span carries an <svg> (a wifi icon on the live site); the
# parser must drop it so it's never mistaken for the location.
_REMOTE = ('<span class="inline-flex items-center gap-x-2"> Hybrid '
           '<svg class="svg-inline--fa fa-wifi"></svg></span>')
_DIV = '<span class="mx-[2px]">\u00b7</span>'


def _card(jid, title, location, dept=None, slug="role"):
    """One <li> job card in Teamtailor's real markup. When dept is None the
    department span is omitted entirely (as the live page does for roles with no
    department) — the key shape the structural parser must handle."""
    meta = ""
    if dept is not None:
        meta += f"<span>{dept}</span>{_DIV}"
    meta += f"<span>{location}</span>{_DIV}{_REMOTE}"
    return (
        '<li class="w-full">'
        '<div class="relative flex flex-col items-center py-6 text-center">'
        f'<a class="@sm:line-clamp-2 flex" data-turbo="false" '
        f'href="https://{HOST}/jobs/{jid}-{slug}">'
        '<span class="absolute inset-0"></span>'
        f'\n      {title}\n</a>'
        f'<div class="mt-1 text-md">{meta}</div>'
        '</div>'
        '<span class="block w-full h-px bg-gradient-block-base-border"></span>'
        '</li>')


def _page(cards):
    return ('<html><body><div class="jobs-list-container">'
            '<ul id="jobs_list_container" class="company-links">'
            + "".join(cards) +
            '</ul></div></body></html>')


_EMPTY = ('<html><body><ul id="jobs_list_container" class="company-links">'
          '</ul></body></html>')


_pages = {}          # page number -> html
_requested = []
def _install(pages):
    """pages: {1: html, 2: html, ...}. Page 1 served for /jobs (no ?page=)."""
    global _pages, _requested
    _pages = pages
    _requested = []
    def fake_get(url, headers=None):
        _requested.append(url)
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(url).query)
        page = int(q.get("page", ["1"])[0])
        return _pages.get(page, _EMPTY)
    connectors.http_get = fake_get
    connectors._polite_pause = lambda: None


def test_parses_real_shape():
    _install({1: _page([
        _card("7860450", "IT &amp; Business Systems Support Analyst (6 month FTC)",
              "Yoto HQ - UK", dept="Technology",
              slug="it-business-systems-support-analyst-6-month-ftc")])})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from /jobs/<id>", j["id"] == "7860450")
    check("title entity unescaped",
          j["title"] == "IT & Business Systems Support Analyst (6 month FTC)")
    check("department from first meta span", j["department"] == "Technology")
    check("location from meta span", j["location"] == "Yoto HQ - UK")
    check("url is the absolute tenant href",
          j["url"] == "https://careers.yotoplay.com/jobs/7860450-"
          "it-business-systems-support-analyst-6-month-ftc")


def test_department_absent_reads_location_not_department():
    # The live "Senior Data Scientist" card has NO department span — its single
    # meta span is the LOCATION. A positional "first span = department" parser
    # would wrongly read the location as the department; this guards that.
    _install({1: _page([
        _card("7896142", "Senior Data Scientist", "Yoto HQ - UK", dept=None,
              slug="senior-data-scientist")])})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("department blank when no department span", jobs[0]["department"] == "")
    check("location still read correctly", jobs[0]["location"] == "Yoto HQ - UK")


def test_mixed_cards_department_present_and_absent():
    _install({1: _page([
        _card("1", "No Dept Role", "Yoto HQ - UK", dept=None),
        _card("2", "Growth Role", "Yoto HQ - UK", dept="Growth"),
        _card("3", "Content Role", "Yoto HQ - UK", dept="Content"),
    ])})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("all three parsed", len(jobs) == 3)
    check("ids in order", [j["id"] for j in jobs] == ["1", "2", "3"])
    check("departments read per-card",
          [j["department"] for j in jobs] == ["", "Growth", "Content"])
    check("locations all correct",
          all(j["location"] == "Yoto HQ - UK" for j in jobs))


def test_remote_status_not_mistaken_for_location():
    # The remote-status span (with the <svg>) must be dropped, not read as a field.
    _install({1: _page([_card("9", "Role", "London", dept="Eng")])})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("location is the place, not 'Hybrid'", jobs[0]["location"] == "London")
    check("department is Eng, location is London",
          jobs[0]["department"] == "Eng" and jobs[0]["location"] == "London")


def test_pages_until_empty():
    # page 1 has 3 cards, page 2 has 2, page 3 empty -> stop; total 5.
    p1 = _page([_card(str(i), f"R{i}", "Yoto HQ - UK", dept="Tech") for i in range(3)]
               ) if False else _page(
        [_card(str(i), f"R{i}", "Yoto HQ - UK", dept="Tech") for i in range(3)])
    p2 = _page([_card(str(10 + i), f"R{10+i}", "Yoto HQ - UK", dept="Tech")
                for i in range(2)])
    _install({1: p1, 2: p2})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("collected 5 across 2 pages", len(jobs) == 5)
    check("requested page 2", any("page=2" in u for u in _requested))
    check("stopped after empty page 3", any("page=3" in u for u in _requested))


def test_empty_list_no_jobs():
    _install({1: _EMPTY})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("empty container -> no jobs, no crash", jobs == [])


def test_row_without_link_skipped():
    bad = ('<li class="w-full"><div class="mt-1 text-md">'
           '<span>Technology</span></div></li>')
    good = _card("5", "Good Role", "Yoto HQ - UK", dept="Tech")
    _install({1: _page([bad, good])})
    jobs = connectors.teamtailor({"url": LIST_URL})
    check("card without a title link skipped", len(jobs) == 1)
    check("good card kept", jobs[0]["id"] == "5")


def test_derives_host_from_pasted_url():
    captured = {}
    def fake_get(url, headers=None):
        captured.setdefault("first", url)
        return _EMPTY
    connectors.http_get = fake_get
    connectors._polite_pause = lambda: None
    connectors.teamtailor({"url": "https://jobs.acme.io/jobs?foo=bar"})
    check("fetches <host>/jobs from the pasted url",
          captured["first"] == "https://jobs.acme.io/jobs")


def test_requires_url():
    try:
        connectors.teamtailor({})
        check("missing url should raise", False)
    except connectors.ConnectorError:
        check("missing url raises ConnectorError", True)


def test_dedupes_by_id():
    merged = connectors._merge_by_id([
        {"id": "same", "title": "X", "location": "London", "department": "", "url": ""},
        {"id": "same", "title": "X", "location": "Berlin", "department": "", "url": ""},
    ])
    check("dup id merged to one", len(merged) == 1)
    check("locations merged", merged[0]["location"] == "London; Berlin")


def test_detect_recognises_teamtailor_native_host():
    r = detect.detect("https://career.teamtailor.com/jobs")
    check("detect provider teamtailor", r["provider"] == "teamtailor")
    check("detect tier 1 runnable", r["tier"] == 1)
    check("detect kept url", r["config"].get("url") == "https://career.teamtailor.com/jobs")
    check("detect kept host", r["config"].get("host") == "career.teamtailor.com")


def test_detect_allowlisted_custom_domain_is_tier1():
    # Yoto's custom domain is on the confirmed allow-list, so it detects as a
    # runnable Tier-1 board (the app's add flow is URL-only — this is what lets
    # Yoto be added by pasting, instead of dead-ending at the Tier-3 guide).
    r = detect.detect("https://careers.yotoplay.com/jobs")
    check("allow-listed custom domain -> teamtailor", r["provider"] == "teamtailor")
    check("allow-listed custom domain -> tier 1", r["tier"] == 1)
    check("kept the full url", r["config"].get("url") == "https://careers.yotoplay.com/jobs")


def test_detect_cazoo_custom_domain_is_tier1():
    # Cazoo is a second confirmed Teamtailor custom domain on the allow-list.
    r = detect.detect("https://careers.cazoo.co.uk/jobs?location_id=325876")
    check("cazoo -> teamtailor", r["provider"] == "teamtailor")
    check("cazoo -> tier 1", r["tier"] == 1)
    check("cazoo kept the full url with query",
          r["config"].get("url") == "https://careers.cazoo.co.uk/jobs?location_id=325876")


def test_detect_unknown_custom_domain_is_tier3():
    # A custom domain NOT on the allow-list can't be told apart from any other
    # site by URL alone, so it stays an honest Tier 3 until confirmed + added.
    r = detect.detect("https://careers.someunknownco.com/jobs")
    check("unknown custom domain -> provider None", r["provider"] is None)
    check("unknown custom domain -> tier 3", r["tier"] == 3)


def test_teamtailor_in_registry_requires_url():
    check("teamtailor registered", "teamtailor" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["teamtailor"]
    check("teamtailor requires url config", required == ["url"])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
