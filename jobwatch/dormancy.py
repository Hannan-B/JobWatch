"""
dormancy.py  (Phase D.2 — the "have I been away too long?" rule)
================================================================
This file answers one question the rest of the app trusts:

    Given when a phase was last checked, has it gone DORMANT?

A phase is dormant when too many days have passed since its last check. "Too
many" is the dormancy threshold from settings.py (default 21 days, changeable).

Why this matters — the locked baseline rule:
    - If a phase is still WARM (checked recently), a new check compares against
      the last check, as normal: you see what's new and removed since then.
    - If a phase has gone DORMANT, coming back starts a CLEAN BASELINE: the next
      check treats everything as new, with no scary "200 removed" churn report
      after a long gap. The "what moved while I was away" question is answered
      by trends later, not by the comparison engine.

That single rule replaces every earlier idea about timelines. There is no
separate "transition window" — switching between active and casual phases while
warm keeps comparing; only dormancy resets the baseline.

This file holds only the *logic*. It does no fetching and writes no files. The
orchestrator (D.3) and phases (D.1) call into it to decide how to behave.
"""

import datetime

from . import settings


def _today() -> datetime.date:
    return datetime.date.today()


def _parse_date(date_str: str) -> datetime.date:
    """Turn 'YYYY-MM-DD' into a date. Raises ValueError on a bad string."""
    return datetime.date.fromisoformat(date_str)


def days_since(last_check_date: str, today: datetime.date | None = None) -> int:
    """
    How many days have passed since the given date. `today` is injectable so
    tests can pretend it's any day (and so the orchestrator can pass a fixed
    'now' for a whole run). Negative results (a future date) are clamped to 0.
    """
    today = today or _today()
    delta = (today - _parse_date(last_check_date)).days
    return max(0, delta)


def is_dormant(last_check_date: str | None,
               today: datetime.date | None = None,
               threshold_days: int | None = None) -> bool:
    """
    Has the phase gone dormant?

    last_check_date - 'YYYY-MM-DD' of the most recent check, or None if the
                      phase has never been checked. A phase that has never been
                      checked is NOT dormant — it's brand new, waiting for its
                      first (baseline) check. Dormancy is about *lapsing*, not
                      about never having started.
    today           - optional override for "now" (testing / fixed run clock).
    threshold_days  - optional override; defaults to the setting (21).

    Returns True if the gap since the last check is at or beyond the threshold.
    """
    if last_check_date in (None, ""):
        return False  # never checked yet = new, not dormant
    if threshold_days is None:
        threshold_days = settings.dormancy_days()
    return days_since(last_check_date, today=today) >= threshold_days


def should_start_clean_baseline(last_check_date: str | None,
                                today: datetime.date | None = None,
                                threshold_days: int | None = None) -> bool:
    """
    The decision the orchestrator actually needs: should the next check in this
    phase be a CLEAN BASELINE (everything new, no comparison), or a normal
    comparison against the last check?

    Clean baseline when the phase is dormant. Otherwise, compare normally.

    Note: a brand-new phase (never checked) is also a clean baseline by nature —
    there's simply nothing to compare against — but that case is already handled
    by the comparison engine's existing baseline logic (compare.is_baseline).
    Here we specifically catch the "warm vs lapsed" decision for a phase that
    HAS history. We return True for the never-checked case too, so the caller
    gets one consistent answer: True = treat as all-new.
    """
    if last_check_date in (None, ""):
        return True  # nothing to compare against yet
    return is_dormant(last_check_date, today=today, threshold_days=threshold_days)


def status(last_check_date: str | None,
           today: datetime.date | None = None,
           threshold_days: int | None = None) -> dict:
    """
    A plain-language summary the interface can show without recomputing
    anything. Returns a small dict: whether dormant, days since last check, the
    threshold in force, and a friendly sentence.
    """
    if threshold_days is None:
        threshold_days = settings.dormancy_days()

    if last_check_date in (None, ""):
        return {
            "dormant": False,
            "ever_checked": False,
            "days_since": None,
            "threshold_days": threshold_days,
            "message": "This phase hasn't been checked yet — the first check "
                       "will set the baseline.",
        }

    gap = days_since(last_check_date, today=today)
    dormant = gap >= threshold_days
    if dormant:
        message = (
            f"It's been {gap} days since the last check "
            f"(dormant past {threshold_days}). Coming back will start a clean "
            f"baseline — everything will show as new."
        )
    else:
        message = (
            f"Last checked {gap} day{'s' if gap != 1 else ''} ago. "
            f"The next check will compare against it as normal."
        )
    return {
        "dormant": dormant,
        "ever_checked": True,
        "days_since": gap,
        "threshold_days": threshold_days,
        "message": message,
    }


# Quick manual test:  python3 -m jobwatch.dormancy
if __name__ == "__main__":
    import datetime as _dt
    fixed_today = _dt.date(2026, 6, 16)

    def check(label, last):
        s = status(last, today=fixed_today)
        print(f"{label}: dormant={s['dormant']}, days_since={s['days_since']}")
        print(f"   -> {s['message']}")

    print(f"(Pretending today is {fixed_today}, threshold = "
          f"{settings.dormancy_days()} days)\n")
    check("Never checked     ", None)
    check("Checked 3 days ago", "2026-06-13")
    check("Checked 20 days ago", "2026-05-27")
    check("Exactly 21 days ago", "2026-05-26")
    check("Checked 60 days ago", "2026-04-17")
