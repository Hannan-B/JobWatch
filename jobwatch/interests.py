"""
interests.py  (Phase D support — your ranked interests on disk)
===============================================================
Your "interests" are the three things that shape every run's report:

    keywords_ranked      - ordered list; earlier = higher priority. A role
                           matching your #1 keyword outranks one matching #5.
    locations_allowed    - the hard location gate (e.g. ["London", "Remote UK"]).
    experience_years_max - your comfortable ceiling; roles asking for more get
                           an amber "stretch" flag (never hidden). null = no flag.

This file just reads and writes that record. The filtering/flagging LOGIC lives
in filters.py (apply_all); this is only the storage of your preferences, the
same way settings.py stores the dormancy threshold. The Phase E interests screen
(E.6) will read and write through here.

Interests live in:  ~/JobWatchData/interests.json

The locked Interests shape (see DATA_FORMATS.md):
    {
        "keywords_ranked":      ["strategy", "data", "director"],
        "locations_allowed":    ["London", "Remote UK"],
        "experience_years_max": 8
    }

If the file doesn't exist yet (you haven't set interests), we return a safe
EMPTY record: no location gate, no keywords, no experience flag. With an empty
record the run still works — every role is "shown", just unranked — so the app
is usable before you've configured anything.
"""

import json
from pathlib import Path

from . import paths


# A safe, do-nothing-yet interests record. Empty locations_allowed means the
# location filter keeps everything (no place is excluded) — see the note in
# run_bucket about why that's the honest default before you've set locations.
EMPTY_INTERESTS = {
    "keywords_ranked": [],
    "keywords_mode": "rank",        # M.3 — "rank" (default) or "filter"
    "locations_allowed": [],
    "departments_allowed": [],      # M.4 — multi, contains-match, no cap
    "departments_mode": "filter",   # this session — "filter" (default) or "rank"
    "experience_years_max": None,
}

# M.2 — the location filter is capped at 5 chosen locations (user decision).
MAX_LOCATIONS = 5


class InterestsError(Exception):
    """Raised for interests problems, with a plain-language message safe to show."""


def _interests_file() -> Path:
    return paths.data_root() / "interests.json"


def load_interests() -> dict:
    """
    Read your interests record, or return a safe empty one if you haven't set
    any yet. Always returns a dict with ALL keys present (incl. the M additions
    keywords_mode + departments_allowed), so callers never guard for missing
    fields and older files load forward-compatibly.
    """
    f = _interests_file()
    if not f.exists():
        return dict(EMPTY_INTERESTS)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise InterestsError(
            "Couldn't read your interests file. It may be corrupted. "
            f"(File: {f})"
        ) from e
    if not isinstance(data, dict):
        raise InterestsError("The interests file isn't in the expected format.")
    # Fill any missing keys from the empty template, so the shape is guaranteed.
    merged = dict(EMPTY_INTERESTS)
    merged.update({k: data[k] for k in EMPTY_INTERESTS if k in data})
    return merged


def save_interests(keywords_ranked: list | None = None,
                   locations_allowed: list | None = None,
                   experience_years_max: int | None = None,
                   keywords_mode: str | None = None,
                   departments_allowed: list | None = None,
                   departments_mode: str | None = None) -> dict:
    """
    Save your interests. Any argument left as None keeps its current stored
    value (so you can update one thing without resupplying the others).
    Returns the saved record.

    keywords_mode       - "rank" (default) or "filter" (M.3).
    departments_allowed - list of department terms (contains-match at filter time).
    departments_mode    - "filter" (default) or "rank" (this session — mirrors
                          keywords). Anything else is rejected with a plain message.
    locations_allowed   - capped at MAX_LOCATIONS (5); more raises a plain error.
    """
    current = load_interests()
    if keywords_ranked is not None:
        current["keywords_ranked"] = [str(k).strip() for k in keywords_ranked
                                      if str(k).strip()]
    if keywords_mode is not None:
        current["keywords_mode"] = _validate_mode(keywords_mode)
    if locations_allowed is not None:
        cleaned = [str(l).strip() for l in locations_allowed if str(l).strip()]
        if len(cleaned) > MAX_LOCATIONS:
            raise InterestsError(
                f"You can choose up to {MAX_LOCATIONS} locations. "
                f"You gave {len(cleaned)}."
            )
        current["locations_allowed"] = cleaned
    if departments_allowed is not None:
        current["departments_allowed"] = [str(d).strip() for d in departments_allowed
                                          if str(d).strip()]
    if departments_mode is not None:
        current["departments_mode"] = _validate_mode(departments_mode)
    if experience_years_max is not None:
        current["experience_years_max"] = _validate_experience(experience_years_max)

    paths.ensure_data_dirs()
    _interests_file().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def _validate_mode(value):
    """Keyword mode must be 'rank' or 'filter'."""
    mode = str(value or "").strip().lower()
    if mode not in ("rank", "filter"):
        raise InterestsError(
            f"Keyword mode should be 'rank' or 'filter', not '{value}'."
        )
    return mode


def _validate_experience(value):
    """Allow a positive whole number, or None to disable the flag."""
    if value is None:
        return None
    try:
        years = int(value)
    except (TypeError, ValueError):
        raise InterestsError(
            f"Experience ceiling should be a whole number of years (or blank), "
            f"not '{value}'."
        )
    if years < 0 or years > 60:
        raise InterestsError(
            f"Experience ceiling should be between 0 and 60 years. You gave {years}."
        )
    return years


# Quick manual test:  python3 -m jobwatch.interests
if __name__ == "__main__":
    print("Current interests:", load_interests())
    print("Saving a sample set...")
    save_interests(keywords_ranked=["strategy", "data"],
                   locations_allowed=["London"],
                   experience_years_max=8)
    print("Now:", load_interests())
    print("Updating only keywords (locations/experience kept)...")
    save_interests(keywords_ranked=["design", "research"])
    print("Now:", load_interests())
