"""
test_va.py — Victoria and Albert Museum connector + detect wiring (mocked)
==========================================================================
Verifies the V&A connector parses the server-rendered vacancies HTML: pulls the
FeedLink id, title, department (from data-group), and the location text node in
vacancy__intro (handling both quoted and unquoted forms), unescapes entities,
dedupes, and that detect recognises the vacancies URL. V&A is single-city
(London) and NOT source-side scopable. Sandbox can't reach vam.ac.uk, so HTTP
is mocked.

Run:  python3 test_va.py
"""

from jobwatch import connectors, detect

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def _block(jid, title, dept, location, quoted=False):
    """One vacancy block in the V&A's real markup. `quoted` wraps the location
    text in quotes + spaces, as the live page does for some roles."""
    loc = f'" {location} "' if quoted else location
    return f'''
<div class="vacancy js-vacancy" data-group="{dept}">
  <div class="vacancy__main">
    <div class="vacancy__intro">
      <span class="vacancy__department vacancy__department--small">{dept}</span>
      <span class="separator vacancy__separator"></span>
      {loc}
    </div>
    <h2 class="vacancy__title"> {title} </h2>
    <a target="_blank" class="u-link u-link--arrowed" data-tracking="job_listing"
       href="https://vam.current-vacancies.com/Jobs/FeedLink/{jid}?cid=3279&linkType=1&rsid=24732">Full information</a>
  </div>
  <div class="vacancy__meta">
    <p class="vacancy__meta__item"><span class="vacancy__meta__title">Deadline: </span> 19 July 2026 </p>
  </div>
</div>'''


def _page(blocks):
    return ('<html><body><div class="main vacancies">'
            '<div class="vacancies__all">' + "".join(blocks) + "</div></div></body></html>")


def _install(page_html):
    connectors.http_get = lambda url, headers=None: page_html


def test_parses_real_shape():
    _install(_page([_block("4233331", "Curator of Dance",
                           "Performance, Furniture, Textiles and Fashion",
                           "Cromwell Road, London", quoted=True)]))
    jobs = connectors.va({})
    check("one job parsed", len(jobs) == 1)
    j = jobs[0]
    check("id from FeedLink", j["id"] == "4233331")
    check("title parsed + trimmed", j["title"] == "Curator of Dance")
    check("department from data-group",
          j["department"] == "Performance, Furniture, Textiles and Fashion")
    check("location from intro text (quoted)", j["location"] == "Cromwell Road, London")
    check("url is the FeedLink", j["url"].startswith(
        "https://vam.current-vacancies.com/Jobs/FeedLink/4233331"))


def test_unquoted_location():
    _install(_page([_block("4238670", "Archives Assistant", "Collections",
                           "Queen Elizabeth Olympic Park, Stratford, London")]))
    jobs = connectors.va({})
    check("location parsed when unquoted",
          jobs[0]["location"] == "Queen Elizabeth Olympic Park, Stratford, London")


def test_multiple_roles_and_meta_boundary():
    # Three roles, each followed by a vacancy__meta div — the parser must not
    # bleed one role's fields into the next.
    page = _page([
        _block("1", "Role One", "Estate", "Cromwell Road, London", quoted=True),
        _block("2", "Role Two", "VARI", "Cromwell Road, London"),
        _block("3", "Role Three", "Collections", "Cromwell Road, London", quoted=True),
    ])
    _install(page)
    jobs = connectors.va({})
    check("all three parsed", len(jobs) == 3)
    check("ids in order", [j["id"] for j in jobs] == ["1", "2", "3"])
    check("titles distinct", [j["title"] for j in jobs] == ["Role One", "Role Two", "Role Three"])
    check("departments distinct",
          [j["department"] for j in jobs] == ["Estate", "VARI", "Collections"])


def test_html_entities_unescaped():
    _install(_page([_block("9", "Curator &amp; Conservator", "Collections",
                           "Cromwell Road, London", quoted=True)]))
    jobs = connectors.va({})
    check("title entity unescaped", jobs[0]["title"] == "Curator & Conservator")


def test_skips_block_without_id():
    # A malformed block with no FeedLink id is skipped, not crashed on.
    bad = '''
<div class="vacancy js-vacancy" data-group="Estate">
  <div class="vacancy__main">
    <div class="vacancy__intro"><span class="vacancy__department">Estate</span>
      <span class="separator vacancy__separator"></span> Cromwell Road, London </div>
    <h2 class="vacancy__title"> No Link Role </h2>
  </div>
</div>'''
    good = _block("5", "Good Role", "Collections", "Cromwell Road, London", quoted=True)
    _install(_page([bad, good]))
    jobs = connectors.va({})
    check("malformed block skipped, good one kept", len(jobs) == 1)
    check("kept the role with an id", jobs[0]["id"] == "5")


def test_empty_page_yields_no_jobs():
    _install("<html><body><div class='vacancies__all'></div></body></html>")
    jobs = connectors.va({})
    check("empty page -> no jobs, no crash", jobs == [])


def test_detect_recognises_va():
    for url in ["https://www.vam.ac.uk/vacancies",
                "https://www.vam.ac.uk/vacancies/"]:
        r = detect.detect(url)
        check(f"detect va: {url[:40]}", r["provider"] == "va")


def test_va_in_connectors_registry():
    check("va registered", "va" in connectors.CONNECTORS)
    func, required, _desc = connectors.CONNECTORS["va"]
    check("va needs no required config", required == [])


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
