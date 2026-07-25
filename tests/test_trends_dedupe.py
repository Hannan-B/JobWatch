"""
test_trends_dedupe.py — same-day duplicate trend rows must not inflate the chart
================================================================================
trends.record_snapshot_trends APPENDS a fresh row set on every run and never
upserts, so checking one company twice in a day writes TWO rows for the same
(company, phase, department, location_bucket, date). server._trends_state sums
rows per (series, phase, date) — correct for the case it was written for (one
series spanning several location buckets, or several companies) — so before the
fix a duplicate silently doubled that date's point. On real data this inflated
6 of 19 dates, two of them tripled: phantom hiring spikes on exactly the days a
check happened to be re-run.

_collapse_duplicate_trend_rows fixes it on the READ side (the engine is sacred;
trends.py is untouched and rows already on disk are corrected as they're read).

These tests lock the four behaviours that matter:
  1. a single check is unchanged (no fix-induced drift),
  2. a same-day re-check no longer doubles open_count,
  3. LEGITIMATE summing still sums (one department across two location buckets,
     and two companies on one date) — the fix must not over-collapse,
  4. deltas still accumulate across a day, while open_count takes the last row.

Run:  python3 -m tests.test_trends_dedupe
"""

from jobwatch import server

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def _row(company="acme", phase="phase-x", dept="Marketing", bucket="London",
         date="2026-07-24", open_count=2, added=None, removed=None,
         phase_type=None):
    """One trend row in the real recorded shape (DATA_FORMATS §6)."""
    e = {"date": date, "company_key": company, "phase_id": phase,
         "department": dept, "location_bucket": bucket, "open_count": open_count}
    if added is not None:
        e["added_count"] = added
    if removed is not None:
        e["removed_count"] = removed
    if phase_type:
        e["phase_type"] = phase_type
    return e


def _collapse(rows):
    return server._collapse_duplicate_trend_rows(rows)


def _cell(rows, dept="Marketing", bucket="London"):
    hits = [e for e in rows
            if e["department"] == dept and e["location_bucket"] == bucket]
    return hits[0] if len(hits) == 1 else None


# --- 1. a single check is untouched --------------------------------------

def test_single_check_unchanged():
    rows = _collapse([_row(open_count=7, added=3, removed=1)])
    check("one row stays one row", len(rows) == 1)
    check("open_count untouched", rows[0]["open_count"] == 7)
    check("added_count untouched", rows[0]["added_count"] == 3)
    check("removed_count untouched", rows[0]["removed_count"] == 1)


def test_distinct_cells_are_not_collapsed():
    rows = _collapse([
        _row(dept="Marketing"), _row(dept="Engineering"),
        _row(dept="Marketing", bucket="Berlin"),
        _row(dept="Marketing", date="2026-07-23"),
        _row(dept="Marketing", company="other-co"),
        _row(dept="Marketing", phase="phase-y"),
    ])
    check("six distinct cells all survive", len(rows) == 6)


# --- 2. the actual bug ----------------------------------------------------

def test_same_day_recheck_does_not_double_open_count():
    # THE BUG: two identical checks on one day. Truth is 2 open, not 4.
    rows = _collapse([_row(open_count=2), _row(open_count=2)])
    check("two duplicate rows collapse to one", len(rows) == 1)
    check("open_count is 2, not 4", rows[0]["open_count"] == 2)


def test_three_checks_in_a_day_still_report_the_truth():
    rows = _collapse([_row(open_count=5), _row(open_count=5), _row(open_count=5)])
    check("three duplicates collapse to one", len(rows) == 1)
    check("open_count is 5, not 15", rows[0]["open_count"] == 5)


def test_open_count_takes_the_LAST_row():
    # Roles were removed between the morning and afternoon check: 9 -> 6.
    # The later absolute count is the current truth.
    rows = _collapse([_row(open_count=9), _row(open_count=6)])
    check("last row wins for open_count", rows[0]["open_count"] == 6)


# --- 3. legitimate summing must survive -----------------------------------

def test_same_department_across_two_buckets_still_sums():
    # NOT duplicates: Marketing in London AND Berlin on one date. The aggregator
    # must still see both rows so the department line totals 5.
    rows = _collapse([_row(bucket="London", open_count=2),
                      _row(bucket="Berlin", open_count=3)])
    check("both location buckets kept", len(rows) == 2)
    check("their open_counts still total 5",
          sum(e["open_count"] for e in rows) == 5)


def test_two_companies_on_one_date_still_sum():
    rows = _collapse([_row(company="acme", open_count=4),
                      _row(company="globex", open_count=6)])
    check("both companies kept", len(rows) == 2)
    check("their open_counts still total 10",
          sum(e["open_count"] for e in rows) == 10)


# --- 4. deltas -------------------------------------------------------------

def test_deltas_accumulate_across_a_day():
    # 3 new roles found in the morning, 1 more in the afternoon = 4 that day.
    rows = _collapse([_row(open_count=3, added=3, removed=0),
                      _row(open_count=4, added=1, removed=0)])
    check("collapsed to one row", len(rows) == 1)
    check("added_count sums to 4", rows[0]["added_count"] == 4)
    check("open_count is the later absolute (4)", rows[0]["open_count"] == 4)


def test_removed_deltas_accumulate_too():
    rows = _collapse([_row(open_count=8, added=0, removed=2),
                      _row(open_count=7, added=0, removed=1)])
    check("removed_count sums to 3", rows[0]["removed_count"] == 3)
    check("open_count is the later absolute (7)", rows[0]["open_count"] == 7)


def test_quiet_recheck_leaves_deltas_alone():
    # The common real case: the day's second check compares against the FIRST
    # check's snapshot hours earlier and finds nothing moved.
    rows = _collapse([_row(open_count=6, added=2, removed=1),
                      _row(open_count=6, added=0, removed=0)])
    check("added_count unchanged at 2", rows[0]["added_count"] == 2)
    check("removed_count unchanged at 1", rows[0]["removed_count"] == 1)
    check("open_count not doubled", rows[0]["open_count"] == 6)


# --- backward compatibility + hygiene --------------------------------------

def test_pre_phase_n_rows_gain_no_delta_fields():
    # Rows written before Phase N carry no deltas; readers treat missing as 0
    # and the collapse must not invent the fields.
    rows = _collapse([_row(open_count=3), _row(open_count=3)])
    check("no added_count invented", "added_count" not in rows[0])
    check("no removed_count invented", "removed_count" not in rows[0])
    check("open_count still correct", rows[0]["open_count"] == 3)


def test_phase_type_preserved_from_whichever_row_carries_it():
    rows = _collapse([_row(open_count=2),
                      _row(open_count=2, phase_type="active")])
    check("phase_type picked up from the second row",
          rows[0].get("phase_type") == "active")


def test_undated_rows_pass_through():
    odd = {"company_key": "acme", "phase_id": "phase-x", "department": "X",
           "location_bucket": "London", "open_count": 1}
    rows = _collapse([odd, _row()])
    check("undated row not dropped", len(rows) == 2)


def test_does_not_mutate_caller_rows():
    a = _row(open_count=2, added=1)
    b = _row(open_count=2, added=1)
    _collapse([a, b])
    check("first input row untouched", a["open_count"] == 2 and a["added_count"] == 1)
    check("second input row untouched", b["open_count"] == 2 and b["added_count"] == 1)


def test_first_appearance_order_preserved():
    rows = _collapse([
        _row(dept="Zeta", date="2026-07-20"),
        _row(dept="Alpha", date="2026-07-21"),
        _row(dept="Zeta", date="2026-07-20"),   # duplicate of the first
    ])
    check("two cells out", len(rows) == 2)
    check("Zeta still first", [e["department"] for e in rows] == ["Zeta", "Alpha"])


def test_dead_duplicate_trends_state_is_gone():
    # server.py once defined _trends_state TWICE; the first was a docstring-only
    # stub shadowed by the real one. Guard against it creeping back.
    import inspect
    src = inspect.getsource(server)
    check("only one _trends_state definition",
          src.count("def _trends_state(") == 1)
    check("the surviving one takes the Phase-N params",
          "metric=\"open\"" in inspect.getsource(server._trends_state))


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
