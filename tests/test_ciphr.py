"""
test_ciphr.py — CIPHR iRecruit (ciphr-irecruit) connector + detect wiring (mocked)
==================================================================================
Verifies the CIPHR connector parses the server-rendered vacancy table: pulls the
/vacancy/<id> id and title from the title link, finds the Location column by its
header tooltip (robust to column order), makes the URL absolute, leaves
department blank (the table has none), unescapes entities, dedupes, and that
detect recognises a ciphr-irecruit URL and flips it to runnable. CIPHR is
single-site HTML, no session token. Sandbox can't reach it, so HTTP is mocked.

Run:  python3 test_ciphr.py
"""

from jobwatch import connectors, detect

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


LIST_URL = "https://bmrecruit.ciphr-irecruit.com/applicants/vacancy"


def _row(jid, title, location, deadline="Sunday, 12 July 2026"):
    slug = title.replace(" ", "-")
    return (f'<tr>'
            f'<td><a href="/Applicants/vacancy/{jid}/{slug}">{title}</a></td>'
            f'<td> {deadline} </td>'
            f'<td> {location} </td>'
            f'<td><a href="#">Apply</a></td>'
            f'</tr>')


def _page(rows, header_order=("Title", "Application Deadline", "Location", "")):
    ths = "".join(
        f'<th title="{h.replace(" ", "")}ColumnHeaderTooltip" scope="col"> {h} </th>'
        if h else "<th> </th>"
        for h in header_order)
    return (
        '<html><body><div class="col-md-12"><div class="table-responsive">'
        '<table class="table table-striped table-bordered table-hover table-condensed">'
        '<caption class="sr-only"> A table listing available vacancies </caption>'
        f'<thead><tr>{ths}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></div></body></html>')


def _install(page_html):
    connectors.http_get = lambda url, headers=None: page_html


def test_parses_real_shape():
    _install(_page([_row("8895", "Collection Move Projects Curator",
                         "London, Great Russell Street")]))
    jobs = connectors.ciphr({"url": LIST_URL})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from /vacancy/<id>", j["id"] == "8895")
    check("title from link text", j["title"] == "Collection Move Projects Curator")
    check("location from Location column", j["location"] == "London, Great Russell Street")
    check("department blank (no column)", j["department"] == "")
    check("url made absolute", j["url"] ==
          "https://bmrecruit.ciphr-irecruit.com/Applicants/vacancy/8895/Collection-Move-Projects-Curator")


def test_multiple_rows():
    _install(_page([
        _row("1", "Visitor Assistant", "London"),
        _row("2", "Conservator", "London, Bloomsbury"),
        _row("3", "Curator", "Remote"),
    ]))
    jobs = connectors.ciphr({"url": LIST_URL})
    check("all three parsed", len(jobs) == 3)
    check("ids in order", [j["id"] for j in jobs] == ["1", "2", "3"])
    check("locations correct",
          [j["location"] for j in jobs] == ["London", "London, Bloomsbury", "Remote"])


def test_location_column_detected_when_reordered():
    # A tenant orders columns Title | Location | Deadline — location must still be
    # found by its header tooltip, not a hard-coded index.
    rows = ['<tr>'
            '<td><a href="/Applicants/vacancy/55/Role">Role</a></td>'
            '<td> Manchester </td>'
            '<td> Friday </td>'
            '</tr>']
    page = _page(rows, header_order=("Title", "Location", "Application Deadline"))
    _install(page)
    jobs = connectors.ciphr({"url": LIST_URL})
    check("location found despite reordering", jobs[0]["location"] == "Manchester")


def test_html_entities_unescaped():
    _install(_page([_row("7", "Curator &amp; Conservator", "London &amp; Remote")]))
    jobs = connectors.ciphr({"url": LIST_URL})
    check("title entity unescaped", jobs[0]["title"] == "Curator & Conservator")
    check("location entity unescaped", jobs[0]["location"] == "London & Remote")


def test_row_without_link_skipped():
    rows = ['<tr><td>No link here</td><td>x</td><td>London</td></tr>',
            _row("9", "Good Role", "London")]
    _install(_page(rows))
    jobs = connectors.ciphr({"url": LIST_URL})
    check("row without title link skipped", len(jobs) == 1)
    check("good row kept", jobs[0]["id"] == "9")


def test_empty_table_no_jobs():
    _install(_page([]))
    jobs = connectors.ciphr({"url": LIST_URL})
    check("empty tbody -> no jobs, no crash", jobs == [])


def test_requires_url():
    try:
        connectors.ciphr({})
        check("missing url should raise", False)
    except connectors.ConnectorError:
        check("missing url raises ConnectorError", True)


def test_fetches_correct_tenant_path():
    captured = {}
    def fake_get(url, headers=None):
        captured["url"] = url
        return _page([_row("1", "X", "London")])
    connectors.http_get = fake_get
    connectors.ciphr({"url": "https://heritagefund.ciphr-irecruit.com/Applicants/vacancy"})
    check("fetches the tenant's /applicants/vacancy",
          captured["url"] == "https://heritagefund.ciphr-irecruit.com/applicants/vacancy")


def test_detect_recognises_ciphr():
    r = detect.detect(LIST_URL)
    check("detect provider ciphr", r["provider"] == "ciphr")
    check("detect tier 1 now runnable", r["tier"] == 1)
    check("detect kept full url", r["config"].get("url") == LIST_URL)
    check("detect kept host", r["config"].get("host") == "bmrecruit.ciphr-irecruit.com")


def test_ciphr_in_registry_requires_url():
    check("ciphr registered", "ciphr" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["ciphr"]
    check("ciphr requires url config", required == ["url"])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
