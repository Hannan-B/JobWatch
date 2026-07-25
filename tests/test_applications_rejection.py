"""
test_applications_rejection.py — staged rejections + the corrected response rate
================================================================================
Two linked changes, 2026-07-25.

1. REJECTIONS NOW CARRY THEIR STAGE. `rejected_before_interview` and
   `rejected_after_interview` replace a bare `rejected`, because "the market
   isn't biting" and "I get in the room but don't close" are different problems
   and the old data couldn't tell them apart. Rows written before the split keep
   their bare "rejected" for ever — NOT migrated, exactly as pre-Phase-N trend
   rows keep working without delta fields.

2. THE RESPONSE RATE WAS WRONG. It counted EVERY rejection as a response,
   including auto-rejections that never reached a human, and counted `withdrawn`
   too — which is the applicant's own action, not a reply. It now asks one
   question, in server._employer_engaged: did the EMPLOYER actually engage?

The subtle part, and what most of these tests pin: `withdrawn` and `ghosted`
aren't answered by a blanket rule but by the engagement MARKERS. Withdrawing
after two interview rounds counts; withdrawing before anyone called doesn't.
Ghosted after a screening counts; ghosted in silence doesn't. Legacy rows are
judged the same way, since they carry no stage.

Run:  python3 -m tests.test_applications_rejection
"""

from jobwatch import applications as apps
from jobwatch import server

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def _rec(status="applied", screening=False, rounds=0):
    return {"id": "1", "company_key": "c", "company_name": "C", "title": "T",
            "url": "", "phase_id": "p", "applied_on": "2026-07-01",
            "status": status, "screening_interview": screening,
            "interview_rounds": rounds, "notes": "",
            "last_progress_at": "2026-07-01"}


# --- the status model -----------------------------------------------------

def test_both_staged_statuses_exist_and_are_terminal():
    check("before-interview status defined",
          apps.STATUS_REJECTED_BEFORE_INTERVIEW == "rejected_before_interview")
    check("after-interview status defined",
          apps.STATUS_REJECTED_AFTER_INTERVIEW == "rejected_after_interview")
    check("both terminal",
          apps.STATUS_REJECTED_BEFORE_INTERVIEW in apps.TERMINAL_STATUSES
          and apps.STATUS_REJECTED_AFTER_INTERVIEW in apps.TERMINAL_STATUSES)
    check("both valid statuses",
          apps.STATUS_REJECTED_BEFORE_INTERVIEW in apps.ALL_STATUSES
          and apps.STATUS_REJECTED_AFTER_INTERVIEW in apps.ALL_STATUSES)


def test_legacy_rejected_still_valid_but_not_offered():
    check("legacy value still accepted", apps.STATUS_REJECTED in apps.ALL_STATUSES)
    check("legacy value never offered as a new choice",
          apps.STATUS_REJECTED not in apps.SELECTABLE_STATUSES)
    check("staged ones ARE offered",
          apps.STATUS_REJECTED_BEFORE_INTERVIEW in apps.SELECTABLE_STATUSES
          and apps.STATUS_REJECTED_AFTER_INTERVIEW in apps.SELECTABLE_STATUSES)


def test_is_rejection_covers_all_three():
    check("before counts", apps.is_rejection("rejected_before_interview"))
    check("after counts", apps.is_rejection("rejected_after_interview"))
    check("legacy counts", apps.is_rejection("rejected"))
    check("ghosted is not a rejection", not apps.is_rejection("ghosted"))
    check("withdrawn is not a rejection", not apps.is_rejection("withdrawn"))


def test_transitions_into_staged_rejections_are_legal():
    for live in ("applied", "screening", "interview"):
        for rej in ("rejected_before_interview", "rejected_after_interview"):
            check(f"{live} -> {rej}", apps._is_legal_transition(live, rej))


def test_legacy_row_can_be_corrected_to_a_staged_one():
    # The owner's existing rejections are all pre-interview; they must be able
    # to reclassify them without deleting and re-adding.
    check("rejected -> before_interview allowed",
          apps._is_legal_transition("rejected", "rejected_before_interview"))
    check("rejected -> after_interview allowed",
          apps._is_legal_transition("rejected", "rejected_after_interview"))


def test_staged_rejection_cannot_reopen_into_a_live_status():
    for rej in ("rejected_before_interview", "rejected_after_interview"):
        check(f"{rej} -> screening blocked",
              not apps._is_legal_transition(rej, "screening"))
        check(f"{rej} -> applied blocked",
              not apps._is_legal_transition(rej, "applied"))


def test_staged_rejections_do_not_auto_ghost():
    rows = [_rec("rejected_before_interview"), _rec("rejected_after_interview")]
    for r in rows:
        r["last_progress_at"] = "2020-01-01"     # long overdue
    out, changed = apps.apply_auto_ghost(rows)
    check("terminal rows never auto-ghost", changed == 0)
    check("statuses untouched",
          [r["status"] for r in out] == ["rejected_before_interview",
                                         "rejected_after_interview"])


# --- the response-rate rule ----------------------------------------------

def test_rejection_before_interview_is_NOT_a_response():
    check("pre-interview rejection excluded",
          not server._employer_engaged(_rec("rejected_before_interview")))


def test_rejection_after_interview_IS_a_response():
    check("post-interview rejection counted",
          server._employer_engaged(_rec("rejected_after_interview")))


def test_explicit_stage_beats_a_contradictory_flag():
    # If the row says the rejection came before any interview, that wins over a
    # stray screening flag — the status is the deliberate statement.
    check("explicit pre-interview stage overrides the marker",
          not server._employer_engaged(
              _rec("rejected_before_interview", screening=True, rounds=3)))


def test_silent_application_is_not_a_response():
    check("still-applied, nothing heard", not server._employer_engaged(_rec("applied")))


def test_ghosted_judged_by_engagement_not_by_label():
    check("ghosted in silence is not a response",
          not server._employer_engaged(_rec("ghosted")))
    check("ghosted AFTER a screening is a response",
          server._employer_engaged(_rec("ghosted", screening=True)))


def test_withdrawn_judged_by_engagement_not_by_label():
    # The old formula counted every withdrawal as a response. It's the
    # applicant's own action, so it only counts if the employer engaged first.
    check("withdrew before anyone called -> not a response",
          not server._employer_engaged(_rec("withdrawn")))
    check("withdrew after 2 interview rounds -> a response",
          server._employer_engaged(_rec("withdrawn", rounds=2)))


def test_live_progress_statuses_are_responses():
    for st in ("screening", "interview", "offer"):
        check(f"{st} counts as a response", server._employer_engaged(_rec(st)))


def test_legacy_rejected_falls_back_to_the_markers():
    check("legacy rejection with no markers -> not a response",
          not server._employer_engaged(_rec("rejected")))
    check("legacy rejection with a screening -> a response",
          server._employer_engaged(_rec("rejected", screening=True)))
    check("legacy rejection with interview rounds -> a response",
          server._employer_engaged(_rec("rejected", rounds=1)))


# --- the funnel -----------------------------------------------------------

def test_post_interview_rejection_counts_in_both_funnel_stages():
    r = _rec("rejected_after_interview")
    check("reached screening", server._reached_screening(r))
    check("reached interview", server._reached_interview(r))


def test_pre_interview_rejection_reaches_neither():
    r = _rec("rejected_before_interview")
    check("did not reach screening", not server._reached_screening(r))
    check("did not reach interview", not server._reached_interview(r))


def test_interview_rounds_alone_count_as_reaching_interview():
    check("rounds imply an interview", server._reached_interview(_rec("ghosted", rounds=2)))


# --- the bug, end to end --------------------------------------------------

def test_the_old_overcount_is_gone():
    """Ten applications: 8 auto-rejected before any contact, 1 ghosted in
    silence, 1 rejected after interviewing. The OLD formula counted all 9
    rejections + gave ~90%. The truth is 1 in 10."""
    rows = ([_rec("rejected_before_interview") for _ in range(8)]
            + [_rec("ghosted")]
            + [_rec("rejected_after_interview")])
    engaged = sum(1 for r in rows if server._employer_engaged(r))
    check("only the post-interview rejection counts", engaged == 1)
    check("response rate is 10%, not 90%", round(engaged / len(rows), 2) == 0.10)


def test_withdrawn_no_longer_inflates_the_rate():
    rows = [_rec("withdrawn") for _ in range(5)]
    check("bare withdrawals contribute nothing",
          sum(1 for r in rows if server._employer_engaged(r)) == 0)


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
