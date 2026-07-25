"""
test_cursor.py — Cursor (cursor.com/careers) connector + detect wiring (mocked)
===============================================================================
Verifies the Cursor connector parses the server-rendered <article> cards: pulls
the /careers/<slug> id and the type-base title, reads department + location
STRUCTURALLY from the meta div (the employment-type span is dropped BY VALUE, so
a role with no department can't shift the location out of place), keeps the
board's already-";"-joined multi-site locations intact, makes URLs absolute,
unescapes entities, and — the Cursor-specific gotcha — collapses the DOUBLE
RENDER: cursor.com ships the same list twice (responsive desktop/mobile markup),
so 2N article blocks must yield N roles via _merge_by_id.

Also guards the history: Cursor's old jobs.ashbyhq.com board is dead, so a
cursor.com careers URL must detect as `cursor`, never as `ashby`.

Sandbox can't reach cursor.com, so HTTP is mocked.

Run:  python3 -m tests.test_cursor
"""

from jobwatch import connectors, detect

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


DIV = '<span class="mx-1">·</span>'


def _card(slug, title, location, dept="Engineering", work_type="Full-time"):
    """One <article> job card in Cursor's real markup. dept=None omits the
    department span entirely (the shape the structural parser must survive);
    work_type=None omits the employment-type span."""
    parts = []
    if dept is not None:
        parts.append(f"<span>{dept}</span>")
    if work_type is not None:
        parts.append(f"<span>{work_type}</span>")
    parts.append(f"<span>{location}</span>")
    meta = DIV.join(parts)
    return (
        '<article class="flex grow-1 flex-col">'
        f'<a class="card card--text grow-1" href="/careers/{slug}">'
        '<div class="sm:gap-g1 flex flex-col sm:flex-row sm:items-end sm:justify-between">'
        '<div class="flex min-w-0 grow-1 flex-col">'
        f'<div class="grow-1"><p class="type-base text-theme-text text-pretty">{title}</p></div>'
        f'<div class="text-theme-text-sec flex shrink-0 items-center">{meta}</div>'
        '</div>'
        '<div class="flex shrink-0 items-center"><span class="btn-tertiary">Apply →</span></div>'
        '</div></a></article>')


def _page(cards, double_render=False):
    """The careers page. double_render=True reproduces the live page's behaviour
    of shipping the identical list twice."""
    body = "".join(cards)
    if double_render:
        body += body
    return f'<html><body><main>{body}</main></body></html>'


_requested = []
def _install(page_html):
    global _requested
    _requested = []
    def fake_get(url, headers=None):
        _requested.append((url, headers or {}))
        return page_html
    connectors.http_get = fake_get
    connectors._polite_pause = lambda: None


def test_parses_real_shape():
    _install(_page([_card("design-engineer", "Design Engineer",
                          "San Francisco; New York", dept="Design")]))
    jobs = connectors.cursor({})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id is the slug", j["id"] == "design-engineer")
    check("title from type-base p", j["title"] == "Design Engineer")
    check("department from first meta span", j["department"] == "Design")
    check("location from last meta span", j["location"] == "San Francisco; New York")
    check("url made absolute",
          j["url"] == "https://cursor.com/careers/design-engineer")


def test_double_render_collapsed_to_one_role():
    # THE Cursor gotcha: the live page renders the same list twice. 3 roles ->
    # 6 article blocks -> must still be 3 jobs, not 6.
    cards = [_card("a", "Role A", "London"),
             _card("b", "Role B", "New York"),
             _card("c", "Role C", "Remote")]
    _install(_page(cards, double_render=True))
    jobs = connectors.cursor({})
    check("6 article blocks collapse to 3 roles", len(jobs) == 3)
    check("ids unique and in order", [j["id"] for j in jobs] == ["a", "b", "c"])
    check("duplicate render did not corrupt location",
          [j["location"] for j in jobs] == ["London", "New York", "Remote"])


def test_department_absent_reads_location_not_department():
    # No department span -> the meta is "Full-time · London". A positional
    # "first span = department" parser would call the location "Full-time".
    _install(_page([_card("no-dept", "No Dept Role", "London", dept=None)]))
    jobs = connectors.cursor({})
    check("department blank when no department span", jobs[0]["department"] == "")
    check("location still read correctly", jobs[0]["location"] == "London")


def test_employment_type_never_becomes_a_field():
    _install(_page([
        _card("r1", "Role One", "London", dept="Sales", work_type="Full-time"),
        _card("r2", "Role Two", "Remote", dept="Design", work_type="Contract"),
        _card("r3", "Role Three", "Japan", dept="Legal", work_type="Part-time"),
    ]))
    jobs = connectors.cursor({})
    check("no department is an employment type",
          not any(j["department"].lower() in ("full-time", "contract", "part-time")
                  for j in jobs))
    check("no location is an employment type",
          not any(j["location"].lower() in ("full-time", "contract", "part-time")
                  for j in jobs))
    check("departments read correctly",
          [j["department"] for j in jobs] == ["Sales", "Design", "Legal"])
    check("locations read correctly",
          [j["location"] for j in jobs] == ["London", "Remote", "Japan"])


def test_employment_type_missing_still_reads_both_fields():
    _install(_page([_card("x", "Role X", "Singapore", dept="Ops",
                          work_type=None)]))
    jobs = connectors.cursor({})
    check("department read with no type span", jobs[0]["department"] == "Ops")
    check("location read with no type span", jobs[0]["location"] == "Singapore")


def test_multi_location_semicolon_preserved():
    # The board already ";"-joins multi-site roles; _merge_by_id must not mangle
    # it, and the parts must survive in order for the location filter.
    _install(_page([_card("multi", "Multi Role",
                          "New York; San Francisco; EMEA", dept="Customer Success")]))
    jobs = connectors.cursor({})
    check("one job", len(jobs) == 1)
    check("all three location parts kept in order",
          jobs[0]["location"] == "New York; San Francisco; EMEA")


def test_html_entities_unescaped():
    _install(_page([_card("amp", "Customer Success Strategy &amp; Operations Manager",
                          "San Francisco", dept="Revenue &amp; Ops")]))
    jobs = connectors.cursor({})
    check("title entity unescaped",
          jobs[0]["title"] == "Customer Success Strategy & Operations Manager")
    check("department entity unescaped", jobs[0]["department"] == "Revenue & Ops")


def test_article_without_role_link_skipped():
    bad = ('<article class="flex grow-1 flex-col"><div class="text-theme-text-sec">'
           '<span>Engineering</span></div></article>')
    good = _card("good", "Good Role", "London")
    _install(_page([bad, good]))
    jobs = connectors.cursor({})
    check("article without a /careers/ link skipped", len(jobs) == 1)
    check("good card kept", jobs[0]["id"] == "good")


def test_empty_page_raises_rather_than_reporting_zero():
    # A silent empty list would read as "every role removed" in a real check.
    _install("<html><body><main></main></body></html>")
    try:
        connectors.cursor({})
        check("empty parse should raise", False)
    except connectors.ConnectorError as e:
        check("empty parse raises ConnectorError", True)
        check("error tells the user to re-capture", "re-capture" in str(e).lower())


def test_fetches_the_careers_page_as_html():
    _install(_page([_card("a", "Role A", "London")]))
    connectors.cursor({})
    url, headers = _requested[0]
    check("fetches cursor.com/careers", url == "https://cursor.com/careers")
    check("asks for HTML", "text/html" in headers.get("Accept", ""))


def test_retries_a_failed_fetch_once():
    state = {"calls": 0}
    def flaky(url, headers=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise connectors.ConnectorError("transient")
        return _page([_card("a", "Role A", "London")])
    connectors.http_get = flaky
    connectors._polite_pause = lambda: None
    jobs = connectors.cursor({})
    check("retried once and succeeded", len(jobs) == 1)
    check("two fetch attempts made", state["calls"] == 2)


def test_detect_recognises_cursor():
    for url in ["https://cursor.com/careers",
                "https://cursor.com/careers/design-engineer",
                "https://www.cursor.com/en-US/careers"]:
        r = detect.detect(url)
        check(f"detect cursor: {url[:42]}", r["provider"] == "cursor")
        check(f"tier 1 runnable: {url[:42]}", r["tier"] == 1)


def test_detect_does_not_mistake_cursor_for_ashby():
    # Cursor's old Ashby board is dead; a cursor.com URL must never yield an
    # ashby board token, which would fetch a 404 board.
    r = detect.detect("https://cursor.com/careers")
    check("provider is cursor, not ashby", r["provider"] == "cursor")
    check("no ashby board token in config", "board" not in r["config"])
    check("suggested key is cursor", r["suggested_key"] == "cursor")


def test_cursor_in_registry_needs_no_config():
    check("cursor registered", "cursor" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["cursor"]
    check("cursor needs no required config", required == [])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
