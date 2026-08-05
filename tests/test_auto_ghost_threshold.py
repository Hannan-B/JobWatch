"""
test_auto_ghost_threshold.py — the auto-ghost threshold and its wiring
======================================================================
2026-08-05. The tracker's auto-ghost threshold moved from a hardcoded 14 days
to settings.ghost_after_days() (default 21), adjustable from the Settings
screen. This suite pins three things that would otherwise be easy to break:

1. THE NUMBER. 21 by default, and the boundary is >= (day 21 ghosts, day 20
   doesn't). Includes the specific regression that motivated the change: a row
   silent for 18 days used to close itself and now stays live.

2. THE INDEPENDENCE. dormancy_days and ghost_after_days both default to 21 and
   sit next to each other on the Settings screen. They are unrelated — one is a
   whole phase going unchecked, the other one employer going quiet. A future
   editor "simplifying" them into one number would break the tracker silently,
   so moving one must provably not move the other.

3. THE DESTRUCTIVE FLIP. apply_auto_ghost overwrites `status` and keeps no
   record of the previous value. That is a real limitation with a real cost
   (raising the threshold does NOT un-ghost past rows — it took a hand-written
   backfill). It's pinned here so it stays a KNOWN property rather than being
   rediscovered the hard way.

WRITES NOTHING. Every test uses either the injectable `threshold_days`
parameter or a stubbed settings module, so this suite never touches
~/JobWatchData/settings.json or applications.json. Calling the real
settings.set_ghost_after_days() here would edit the owner's live settings — the
same trap that made test_trends_london_boroughs fail on its second-ever run.

Run:  python3 -m tests.test_auto_ghost_threshold
"""

import datetime

from jobwatch import applications as apps
from jobwatch import settings

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


TODAY = datetime.date(2026, 8, 5)


def _rec(status="applied", days_silent=0, screening=False, rounds=0):
    """A row whose last forward signal was `days_silent` days before TODAY."""
    anchor = (TODAY - datetime.timedelta(days=days_silent)).isoformat()
    return {"id": "1", "company_key": "c", "company_name": "C", "title": "T",
            "url": "", "phase_id": "p", "applied_on": anchor,
            "status": status, "screening_interview": screening,
            "interview_rounds": rounds, "notes": "",
            "last_progress_at": anchor}


class _FakeSettings:
    """Stands in for the settings module so nothing reaches disk."""
    DEFAULTS = settings.DEFAULTS
    def __init__(self, ghost_days): self._g = ghost_days
    def ghost_after_days(self): return self._g


def _with_stubbed_settings(ghost_days, fn):
    """Run fn() with applications' settings dependency swapped out, restoring
    it afterwards even if fn raises."""
    real = apps.settings
    apps.settings = _FakeSettings(ghost_days)
    try:
        return fn()
    finally:
        apps.settings = real


# --- 1. the number --------------------------------------------------------

def test_default_is_21_days():
    check("default threshold is 21", settings.DEFAULTS["ghost_after_days"] == 21)


def test_boundary_is_inclusive_at_the_threshold():
    out, changed = apps.apply_auto_ghost([_rec(days_silent=21)], today=TODAY,
                                         threshold_days=21)
    check("exactly 21 days ghosts", changed == 1 and out[0]["status"] == "ghosted")

    out, changed = apps.apply_auto_ghost([_rec(days_silent=20)], today=TODAY,
                                         threshold_days=21)
    check("20 days does not ghost", changed == 0 and out[0]["status"] == "applied")


def test_the_regression_that_motivated_the_change():
    """The case the change existed for: applications silent for 15-18 days had
    auto-closed under the old 14-day rule while still genuinely live. Under 21
    they stay live."""
    rows = [_rec(days_silent=d) for d in (15, 18, 18, 18, 18)]
    _, changed_old = apps.apply_auto_ghost(rows, today=TODAY, threshold_days=14)
    check("all five WOULD have ghosted at 14", changed_old == 5)
    out, changed_new = apps.apply_auto_ghost(rows, today=TODAY, threshold_days=21)
    check("none ghost at 21", changed_new == 0)
    check("all five still live",
          all(r["status"] == "applied" for r in out))


def test_every_live_rung_can_ghost_not_just_applied():
    rows = [_rec("applied", 25), _rec("screening", 25), _rec("interview", 25)]
    _, changed = apps.apply_auto_ghost(rows, today=TODAY, threshold_days=21)
    check("all three live statuses ghost", changed == 3)


def test_terminal_rows_are_never_touched():
    rows = [_rec(s, 999) for s in ("ghosted", "offer", "withdrawn",
                                   "rejected_before_interview",
                                   "rejected_after_interview", "rejected")]
    out, changed = apps.apply_auto_ghost(rows, today=TODAY, threshold_days=21)
    check("terminal rows never auto-ghost", changed == 0)
    check("terminal statuses unchanged",
          [r["status"] for r in out] == [r["status"] for r in rows])


def test_unparseable_or_missing_anchor_never_ghosts():
    r = _rec(days_silent=99)
    r["last_progress_at"] = ""
    r["applied_on"] = ""
    _, changed = apps.apply_auto_ghost([r], today=TODAY, threshold_days=21)
    check("a row with no usable date is left alone", changed == 0)


def test_apply_auto_ghost_does_not_mutate_the_input():
    rows = [_rec(days_silent=30)]
    apps.apply_auto_ghost(rows, today=TODAY, threshold_days=21)
    check("caller's records untouched (it returns copies)",
          rows[0]["status"] == "applied")


# --- 2. the settings wiring, and independence from dormancy ---------------

def test_threshold_is_read_from_settings_not_hardcoded():
    got = _with_stubbed_settings(30, apps.ghost_after_days)
    check("accessor follows the setting", got == 30)

    out, changed = _with_stubbed_settings(
        30, lambda: apps.apply_auto_ghost([_rec(days_silent=25)], today=TODAY))
    check("a 25-day row does not ghost when the setting says 30", changed == 0)


def test_explicit_override_beats_the_setting():
    _, changed = _with_stubbed_settings(
        365, lambda: apps.apply_auto_ghost([_rec(days_silent=25)],
                                           today=TODAY, threshold_days=21))
    check("threshold_days wins over the stored setting", changed == 1)


def test_a_broken_settings_file_falls_back_to_the_default():
    class _Exploding:
        DEFAULTS = settings.DEFAULTS
        def ghost_after_days(self): raise OSError("settings.json is corrupt")
    real = apps.settings
    apps.settings = _Exploding()
    try:
        check("falls back to 21 rather than raising", apps.ghost_after_days() == 21)
    finally:
        apps.settings = real


def test_ghost_and_dormancy_are_separate_settings():
    check("two distinct keys",
          "ghost_after_days" in settings.DEFAULTS
          and "dormancy_days" in settings.DEFAULTS)
    check("distinct accessors",
          settings.ghost_after_days is not settings.dormancy_days)


def test_moving_one_threshold_does_not_move_the_other():
    """They share a default of 21 and sit together on the Settings screen, which
    makes them easy to conflate. Prove the tracker reads only its own."""
    got = _with_stubbed_settings(30, apps.ghost_after_days)
    check("auto-ghost reads its own setting", got == 30)
    check("dormancy default is untouched at 21",
          settings.DEFAULTS["dormancy_days"] == 21)


def test_validation_rejects_nonsense():
    for bad in ("abc", None, "", 0, -5, 400):
        try:
            settings._validate_ghost_after_days(bad)
            check(f"rejects {bad!r}", False)
        except settings.SettingsError:
            check(f"rejects {bad!r}", True)


def test_validation_accepts_sane_values():
    check("accepts 21", settings._validate_ghost_after_days(21) == 21)
    check("accepts a numeric string (the form field sends text)",
          settings._validate_ghost_after_days("28") == 28)
    check("accepts the bounds",
          settings._validate_ghost_after_days(settings.MIN_GHOST_AFTER_DAYS)
          == settings.MIN_GHOST_AFTER_DAYS
          and settings._validate_ghost_after_days(settings.MAX_GHOST_AFTER_DAYS)
          == settings.MAX_GHOST_AFTER_DAYS)


# --- 3. the destructive flip (a KNOWN limitation, pinned) -----------------

def test_the_flip_destroys_the_previous_status():
    """Not a bug report — a pinned property. A screening row and an applied row
    are indistinguishable once ghosted, which is why raising the threshold
    cannot un-ghost anything automatically. If someone later adds a marker
    recording the prior status, this test SHOULD fail and be rewritten."""
    out, _ = apps.apply_auto_ghost(
        [_rec("applied", 30), _rec("screening", 30)], today=TODAY,
        threshold_days=21)
    check("both read as plain ghosted",
          [r["status"] for r in out] == ["ghosted", "ghosted"])
    leftover = set(out[0].keys()) ^ set(out[1].keys())
    check("no field records what they used to be", not leftover)


def test_un_ghosting_via_the_ladder_is_refused():
    """The corollary: a backfill cannot go through update_status. `ghosted` is
    terminal and the one-way ladder is right to refuse this — any correction is
    a direct file write. Do not weaken the ladder to make a backfill easier."""
    for live in ("applied", "screening", "interview"):
        check(f"ghosted -> {live} blocked",
              not apps._is_legal_transition("ghosted", live))


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
