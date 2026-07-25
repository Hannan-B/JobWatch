"""
test_trends_london_boroughs.py — borough → London in TREND buckets
==================================================================
The location FILTER already folds London boroughs into London (see
test_filters_london_boroughs). Trends are a SEPARATE code path: trends.py's
_bucket_location computes each row's location_bucket from the role's raw
location. Left alone it took the first comma-part verbatim, so a role tagged
"Richmond upon Thames" would form its OWN trend bucket — scattering a company's
London roles across many tiny lines and making a "London" trends filter miss
them.

The fix reuses filters._SITE_CITY so boroughs/sites fold to their city in the
bucket too. These tests lock that: boroughs bucket to "London", the roles
aggregate into ONE London bucket, plain/country buckets are unchanged, and
same-named places elsewhere (Richmond VA, Kingston Jamaica) are NOT folded in.

Run:  python3 test_trends_london_boroughs.py
"""

import uuid

from jobwatch import trends

# SELF-ISOLATION (the fix, 2026-07-24). record_snapshot_trends APPENDS — it does
# not upsert on (company, phase, dept, bucket, date) — so a fixed company/phase
# id accumulates a fresh row set on EVERY run, and this file's "merged into one
# row" assertion fails the second time it is ever run. A unique id per run makes
# the assertion depend only on what THIS run wrote. run_all() then deletes those
# rows, so the real trends.json is left exactly as it was found.
_RUN = uuid.uuid4().hex[:8]
_COMPANY = f"cazoo-test-{_RUN}"
_PHASE = f"phase-btest-{_RUN}"


def _cleanup_rows():
    """Drop this run's rows from trends.json. Never raises — a failed cleanup
    must not fail the suite."""
    try:
        data = trends._load()
        before = len(data.get("entries", []))
        data["entries"] = [e for e in data.get("entries", [])
                           if e.get("company_key") != _COMPANY]
        if len(data["entries"]) != before:
            trends._save(data)
    except Exception:
        pass


_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def _job(loc, dept="", jid="x"):
    return {"id": jid, "title": "R", "location": loc, "department": dept, "url": ""}


def test_borough_buckets_to_london():
    check("Richmond upon Thames -> London",
          trends._bucket_location("Richmond upon Thames") == "London")
    check("Shoreditch -> London", trends._bucket_location("Shoreditch") == "London")
    check("Hackney -> London", trends._bucket_location("Hackney") == "London")
    check("Chip HQ -> London", trends._bucket_location("Chip HQ") == "London")


def test_borough_with_country_suffix_buckets_to_london():
    check("borough + country suffix still folds",
          trends._bucket_location("Richmond upon Thames, United Kingdom") == "London")


def test_national_gallery_site_still_folds():
    check("pre-existing site entry still works",
          trends._bucket_location("The National Gallery") == "London")


def test_plain_city_unchanged():
    check("London, UK -> London",
          trends._bucket_location("London, United Kingdom") == "London")
    check("missing-space London -> London",
          trends._bucket_location("London,United Kingdom") == "London")


def test_country_only_unchanged():
    check("United Kingdom stays country bucket",
          trends._bucket_location("United Kingdom") == "United Kingdom")


def test_blank_is_unknown():
    check("blank -> Unknown", trends._bucket_location("") == "Unknown")


def test_same_named_places_elsewhere_not_folded():
    # The whole point of exact full-name matching: these must NOT become London.
    check("Richmond, VA does NOT fold to London",
          trends._bucket_location("Richmond, VA") == "Richmond")
    check("Kingston, Jamaica does NOT fold to London",
          trends._bucket_location("Kingston, Jamaica") == "Kingston")
    check("Manchester unchanged",
          trends._bucket_location("Manchester") == "Manchester")


def test_boroughs_aggregate_into_one_london_bucket():
    # Two Marketing roles tagged in DIFFERENT boroughs must record as ONE London
    # bucket with open_count 2 — not two scattered borough rows of 1 each.
    jobs = [
        _job("Richmond upon Thames", "Marketing", "1"),
        _job("Shoreditch", "Marketing", "2"),
        _job("Hackney", "Engineering", "3"),
    ]
    trends.record_snapshot_trends(_COMPANY, _PHASE, jobs, date="2026-07-06")
    rows = trends.entries_for(_COMPANY, _PHASE)
    mk = [e for e in rows if e["department"] == "Marketing"]
    check("both borough Marketing roles merge into one row", len(mk) == 1)
    check("merged bucket is London",
          bool(mk) and mk[0]["location_bucket"] == "London")
    check("merged open_count is 2", bool(mk) and mk[0]["open_count"] == 2)
    eng = [e for e in rows if e["department"] == "Engineering"]
    check("engineering borough also buckets to London",
          bool(eng) and eng[0]["location_bucket"] == "London")
    # No stray borough-named buckets leaked through.
    buckets = {e["location_bucket"] for e in rows}
    check("no raw borough buckets recorded", buckets == {"London"})


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    _cleanup_rows()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
