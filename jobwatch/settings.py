"""
settings.py  (Phase D — your adjustable preferences)
====================================================
A tiny store for settings you can change without touching code. It holds the
day-count thresholds the app judges silence by: how long a PHASE can go
unchecked before it counts as "dormant", and how long a single APPLICATION can
sit without a forward signal before the tracker marks it "ghosted".

Why its own file: keeping settings separate means the Phase E settings screen
has one clean place to look, and the logic that uses these numbers (dormancy.py,
applications.py) reads them from here instead of having a literal baked into the
code. Change it once, everywhere respects it.

NOTE — the two thresholds are INDEPENDENT and happen to share a default of 21
days. They answer different questions (a whole phase going quiet vs. one
employer going quiet) and must never be collapsed into one setting or read from
each other's accessor. Changing one must not move the other.

Built deliberately small. Adding another setting later is easy: give it a key,
a default in DEFAULTS, and (optionally) a friendly getter like the one below.
We are NOT pre-building settings we don't need yet — that's the project rule.

Settings live in:  ~/JobWatchData/settings.json   (a simple key -> value map)

Current settings:
    dormancy_days    : int — days with no check before a phase counts as dormant.
    ghost_after_days : int — days with no forward signal on an application before
                             the tracker auto-flips it to "ghosted".
"""

import json
from pathlib import Path

from . import paths


# The single source of truth for default values. A setting that has never been
# changed simply isn't in the file, and we fall back to the default here. This
# is what makes the store safe to grow: a new key with a new default is one line.
DEFAULTS = {
    "dormancy_days": 21,
    # Raised from 14 to 21 (2026-08-05). Two weeks of silence turned out to
    # close applications that were still genuinely alive.
    "ghost_after_days": 21,
}

# Guard rails for values a person might set, so a typo can't break the logic.
# (Plain-language messages; this app's user is not a coder.)
MIN_DORMANCY_DAYS = 1
MAX_DORMANCY_DAYS = 365

MIN_GHOST_AFTER_DAYS = 1
MAX_GHOST_AFTER_DAYS = 365


class SettingsError(Exception):
    """Raised for settings problems, with a plain-language message safe to show."""


def _settings_file() -> Path:
    return paths.data_root() / "settings.json"


def _load_all() -> dict:
    """Read the whole settings map. Returns {} if the file doesn't exist yet."""
    f = _settings_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SettingsError(
            "Couldn't read your settings file. It may be corrupted. "
            f"(File: {f})"
        ) from e
    if not isinstance(data, dict):
        raise SettingsError("The settings file isn't in the expected format.")
    return data


def _save_all(values: dict) -> None:
    """Write the full settings map back to disk (creates the folder if needed)."""
    paths.ensure_data_dirs()
    _settings_file().write_text(json.dumps(values, indent=2), encoding="utf-8")


def get(key: str):
    """
    Return the current value for a setting, or its default if it's never been
    set. Unknown keys raise, so a typo is caught loudly rather than silently
    returning None.
    """
    if key not in DEFAULTS:
        raise SettingsError(f"Unknown setting '{key}'.")
    return _load_all().get(key, DEFAULTS[key])


def set(key: str, value) -> None:
    """Change one setting and save. Validates known settings before writing."""
    if key not in DEFAULTS:
        raise SettingsError(f"Unknown setting '{key}'.")
    if key == "dormancy_days":
        value = _validate_dormancy_days(value)
    elif key == "ghost_after_days":
        value = _validate_ghost_after_days(value)
    values = _load_all()
    values[key] = value
    _save_all(values)


def all_settings() -> dict:
    """
    Return every setting with its current value (defaults filled in for any not
    explicitly set). Handy for a settings screen that wants to show everything.
    """
    stored = _load_all()
    return {k: stored.get(k, default) for k, default in DEFAULTS.items()}


# ---- friendly, named accessors (clearer than get("dormancy_days") at call sites) ----

def dormancy_days() -> int:
    """How many days of no checks before a phase is considered dormant."""
    return int(get("dormancy_days"))


def set_dormancy_days(days: int) -> None:
    """Change the dormancy threshold (e.g. from 21 to 28). Validated."""
    set("dormancy_days", days)


def ghost_after_days() -> int:
    """
    How many days an application can sit with no forward signal before the
    tracker auto-flips it to "ghosted".

    Deliberately SEPARATE from dormancy_days even though both default to 21:
    one is about a whole phase going quiet, the other about a single employer
    going quiet. Never implement one by calling the other.
    """
    return int(get("ghost_after_days"))


def set_ghost_after_days(days: int) -> None:
    """Change the auto-ghost threshold (e.g. from 21 to 28). Validated."""
    set("ghost_after_days", days)


def _validate_day_count(value, label: str, lo: int, hi: int) -> int:
    """Shared rule for every whole-number-of-days setting, so a new threshold
    can't quietly grow its own looser validation."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise SettingsError(
            f"The {label} setting needs to be a whole number of days, "
            f"not '{value}'."
        )
    if days < lo or days > hi:
        raise SettingsError(
            f"The {label} setting should be between {lo} and {hi} days. "
            f"You gave {days}."
        )
    return days


def _validate_dormancy_days(value) -> int:
    return _validate_day_count(value, "dormancy",
                               MIN_DORMANCY_DAYS, MAX_DORMANCY_DAYS)


def _validate_ghost_after_days(value) -> int:
    return _validate_day_count(value, "auto-ghost",
                               MIN_GHOST_AFTER_DAYS, MAX_GHOST_AFTER_DAYS)


# Quick manual test:  python3 -m jobwatch.settings
if __name__ == "__main__":
    print("Default dormancy_days:", dormancy_days())
    print("Setting it to 28...")
    set_dormancy_days(28)
    print("Now dormancy_days:", dormancy_days())
    print("All settings:", all_settings())
    print("Resetting to 21...")
    set_dormancy_days(21)
    print("Back to:", dormancy_days())
