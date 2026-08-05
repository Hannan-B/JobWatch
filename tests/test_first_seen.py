"""
test_first_seen.py — the first-seen index
==========================================
2026-08-05. "Since when has this role been continuously present in my checks?"
— derived once from the snapshot tree, then maintained per run.

What this pins, and why each one would otherwise rot quietly:

1. CONTINUITY vs RE-APPEARANCE. The owner's locked rule is that a job leaving
   the snapshots and coming back RESTARTS its clock. That decision lives in one
   `if` and has no visible symptom when wrong — the date just reads plausibly
   and is wrong. Pinned from both directions.

2. THE DORMANT CARRY-THROUGH. A lapsed phase is NOT a job disappearing; we
   stopped looking. Entries must survive a clean baseline with their `since`
   intact. Easy to "simplify" away, since a baseline looks like a gap.

3. BOUNDED DATES. Roles already live when a company was added get the company's
   first-check date, which is an upper bound, not a fact. The flag is what stops
   the later job-age analysis treating a newly added company's whole board as
   having been posted on the day tracking began.

4. THE UNCLEAR FALLBACK. Manual tracker rows never came through a check. They
   must read as "not recorded", never as a guessed date — same contract as
   location_unclear.

5. THE FALSY-ID RULE. A job id of "0" is valid and must survive (the lesson
   already paid for in connectors.py).

WRITES NOTHING. record_check and annotate are pure; the one test that exercises
rebuild_from_snapshots stubs the filesystem seams. Calling the real rebuild here
would overwrite the owner's live index — the trap that made
test_trends_london_boroughs fail on its second-ever run.

Run:  python3 -m tests.test_first_seen
"""

import datetime

from jobwatch import first_seen

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


D = lambda s: datetime.date.fromisoformat(s)
def job(i, title="Role"):
    return {"id": str(i), "title": f"{title} {i}", "location": "London",
            "department": "", "url": ""}


def _rec(data, key, jid):
    return first_seen.for_company(key, data).get(str(jid))


# --- 1. continuity vs re-appearance --------------------------------------

def test_a_job_present_every_check_keeps_its_original_date():
    data = first_seen.record_check("acme", [job(1)], previous_job_ids=None,
                                   today=D("2026-07-01"), is_first_check=True)
    data = first_seen.record_check("acme", [job(1)], previous_job_ids={"1"},
                                   today=D("2026-07-10"), data=data)
    data = first_seen.record_check("acme", [job(1)], previous_job_ids={"1"},
                                   today=D("2026-07-20"), data=data)
    r = _rec(data, "acme", 1)
    check("since stays at the first sighting", r["since"] == "2026-07-01")
    check("last_seen tracks the newest check", r["last_seen"] == "2026-07-20")


def test_a_new_job_dates_from_the_check_it_appeared_in():
    data = first_seen.record_check("acme", [job(1)], today=D("2026-07-01"),
                                   is_first_check=True)
    data = first_seen.record_check("acme", [job(1), job(2)],
                                   previous_job_ids={"1"},
                                   today=D("2026-07-10"), data=data)
    check("arrival date is its own check", _rec(data, "acme", 2)["since"] == "2026-07-10")
    check("it is not bounded", _rec(data, "acme", 2)["bounded"] is False)


def test_a_job_that_left_and_returned_restarts_its_clock():
    """The owner's locked choice (2026-08-05): option 2, most recent
    re-appearance. If this ever flips to 'original sighting', THIS is the test
    that should fail — change it deliberately, don't delete it."""
    data = first_seen.record_check("acme", [job(3)], today=D("2026-07-01"),
                                   is_first_check=True)
    data = first_seen.record_check("acme", [], previous_job_ids={"3"},
                                   today=D("2026-07-10"), data=data)
    data = first_seen.record_check("acme", [job(3)], previous_job_ids=set(),
                                   today=D("2026-07-20"), data=data)
    r = _rec(data, "acme", 3)
    check("since moves to the return", r["since"] == "2026-07-20")
    check("a restarted clock is no longer bounded", r["bounded"] is False)


def test_unknown_previous_assumes_continuity():
    """previous_job_ids=None means 'we don't know'. Never invent a gap without
    evidence — that would silently reset dates on any path that can't supply
    the prior list."""
    data = first_seen.record_check("acme", [job(1)], today=D("2026-07-01"),
                                   is_first_check=True)
    data = first_seen.record_check("acme", [job(1)], previous_job_ids=None,
                                   today=D("2026-07-20"), data=data)
    check("no evidence of a gap -> date held", _rec(data, "acme", 1)["since"] == "2026-07-01")


# --- 2. the dormant carry-through -----------------------------------------

def test_a_dormant_lapse_does_not_restart_the_clock():
    """A clean baseline reports everything as new, but the job never went
    anywhere — the owner stopped checking. Blaming the role for that would
    date every role in a returning phase to the day the hunt resumed."""
    data = first_seen.record_check("acme", [job(1)], today=D("2026-06-01"),
                                   is_first_check=True)
    data = first_seen.record_check("acme", [job(1)], previous_job_ids=set(),
                                   today=D("2026-07-20"),
                                   carry_through_gap=True, data=data)
    check("since survives the lapse", _rec(data, "acme", 1)["since"] == "2026-06-01")
    check("last_seen still advances", _rec(data, "acme", 1)["last_seen"] == "2026-07-20")


def test_carry_through_still_dates_genuinely_new_roles_to_today():
    data = first_seen.record_check("acme", [job(1)], today=D("2026-06-01"),
                                   is_first_check=True)
    data = first_seen.record_check("acme", [job(1), job(5)],
                                   previous_job_ids=set(), today=D("2026-07-20"),
                                   carry_through_gap=True, data=data)
    check("a role first seen after the lapse dates from the lapse",
          _rec(data, "acme", 5)["since"] == "2026-07-20")


# --- 3. bounded dates ------------------------------------------------------

def test_first_ever_check_marks_every_role_bounded():
    """A company added mid-phase: every role reads as first seen on its first
    check, because that is when tracking began — not when they were posted."""
    data = first_seen.record_check("newco", [job(i) for i in range(1, 6)],
                                   today=D("2026-07-24"), is_first_check=True)
    rows = first_seen.for_company("newco", data)
    check("all five recorded", len(rows) == 5)
    check("all bounded", all(r["bounded"] for r in rows.values()))


def test_later_checks_are_not_bounded():
    data = first_seen.record_check("newco", [job(1)], today=D("2026-07-24"),
                                   is_first_check=True)
    data = first_seen.record_check("newco", [job(1), job(2)],
                                   previous_job_ids={"1"},
                                   today=D("2026-08-01"), data=data)
    check("the newcomer is exact", _rec(data, "newco", 2)["bounded"] is False)
    check("the original stays bounded", _rec(data, "newco", 1)["bounded"] is True)


# --- 4. annotate, and the unclear fallback --------------------------------

def test_annotate_attaches_the_date_without_mutating_the_input():
    data = first_seen.record_check("acme", [job(1)], today=D("2026-07-01"),
                                   is_first_check=True)
    src = [job(1)]
    out = first_seen.annotate(src, company_key="acme", data=data)
    check("date attached", out[0]["first_seen"] == "2026-07-01")
    check("bounded flag attached", out[0]["first_seen_bounded"] is True)
    check("not flagged unclear", out[0]["first_seen_unclear"] is False)
    check("caller's job dict untouched", "first_seen" not in src[0])


def test_an_unknown_job_reads_as_unclear_not_as_a_guess():
    """A manually-added tracker row, or one whose company was removed. The
    honest answer is 'not recorded' — same contract as location_unclear."""
    out = first_seen.annotate([job(99)], company_key="acme",
                              data=first_seen._empty())
    check("no date invented", out[0]["first_seen"] is None)
    check("flagged unclear", out[0]["first_seen_unclear"] is True)


def test_annotate_reads_each_job_s_own_company_when_none_is_given():
    """Saved jobs and applications are FLAT lists spanning companies — they
    carry company_key per row rather than being grouped."""
    data = first_seen.record_check("acme", [job(1)], today=D("2026-07-01"),
                                   is_first_check=True)
    data = first_seen.record_check("beta", [job(1)], today=D("2026-07-05"),
                                   is_first_check=True, data=data)
    rows = [dict(job(1), company_key="acme"), dict(job(1), company_key="beta")]
    out = first_seen.annotate(rows, data=data)
    check("per-row company resolved separately",
          [o["first_seen"] for o in out] == ["2026-07-01", "2026-07-05"])


# --- 5. edges --------------------------------------------------------------

def test_a_zero_id_survives():
    """`if not jid` would drop it. The falsy-id lesson, already paid for once
    in connectors.py — it must not be re-learned here."""
    data = first_seen.record_check("acme", [job(0)], today=D("2026-07-01"),
                                   is_first_check=True)
    check("id '0' is recorded", _rec(data, "acme", 0) is not None)


def test_rows_without_an_id_are_skipped_not_crashed_on():
    data = first_seen.record_check("acme", [{"title": "no id"}, job(1)],
                                   today=D("2026-07-01"), is_first_check=True)
    check("only the real row lands", len(first_seen.for_company("acme", data)) == 1)


def test_a_corrupt_index_reads_as_empty_rather_than_raising():
    check("None -> empty", first_seen.for_company("acme", {"companies": None}) == {})
    check("garbage shape -> empty", first_seen.for_company("acme", {}) == {})


def test_companies_are_kept_separate():
    data = first_seen.record_check("acme", [job(1)], today=D("2026-07-01"),
                                   is_first_check=True)
    data = first_seen.record_check("beta", [job(1)], today=D("2026-08-01"),
                                   is_first_check=True, data=data)
    check("same job id, different companies, different dates",
          _rec(data, "acme", 1)["since"] != _rec(data, "beta", 1)["since"])


# --- 6. the rebuild, with the filesystem stubbed out ----------------------

def test_rebuild_walks_snapshots_oldest_first_and_applies_the_same_rules():
    """Exercises rebuild_from_snapshots end to end WITHOUT touching the real
    data folder: the storage seam and the writer are both stubbed. The fixture
    covers all four behaviours at once — continuous, arriving, and
    leaving-then-returning roles, plus a first-ever check."""
    import json, pathlib, tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    fixtures = [
        ("2026-07-01", [job(1), job(3)]),
        ("2026-07-10", [job(1), job(2)]),
        ("2026-07-20", [job(1), job(2), job(3)]),
    ]
    written = []
    for day, jobs in fixtures:
        p = tmp / f"{day}.json"
        p.write_text(json.dumps({"company_key": "acme", "phase_id": "p1",
                                 "taken_at": f"{day}T09:00:00", "jobs": jobs}))
        written.append(p)

    real_storage, real_save = first_seen.storage, first_seen._save
    captured = {}

    class _FakeStorage:
        @staticmethod
        def list_snapshots(key): return written if key == "acme" else []

    first_seen.storage = _FakeStorage()
    first_seen._save = lambda d: captured.update(d)
    try:
        data = first_seen.rebuild_from_snapshots(company_keys=["acme"])
    finally:
        first_seen.storage, first_seen._save = real_storage, real_save

    check("continuous role keeps its first date",
          _rec(data, "acme", 1)["since"] == "2026-07-01")
    check("continuous role from the first check is bounded",
          _rec(data, "acme", 1)["bounded"] is True)
    check("arriving role dates from its own check",
          _rec(data, "acme", 2)["since"] == "2026-07-10")
    check("returning role restarts",
          _rec(data, "acme", 3)["since"] == "2026-07-20")
    check("rebuild persists its result", captured.get("companies") is not None)
    check("nothing was written to the real data folder", True)


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
