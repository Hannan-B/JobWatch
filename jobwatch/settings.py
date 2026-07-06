"""
settings.py  (Phase D — your adjustable preferences)
====================================================
A tiny store for settings you can change without touching code. Right now it
holds exactly one thing: how many days of silence count as "dormant". The
default is 21 days. You can change it later (e.g. to 28) from the settings
screen once the interface exists — this file is what that screen will read and
write underneath.

Why its own file: keeping settings separate means the Phase E settings screen
has one clean place to look, and the dormancy logic (dormancy.py) reads the
number from here instead of having "21" baked into the code. Change it once,
everywhere respects it.

Built deliberately small. Adding another setting later is easy: give it a key,
a default in DEFAULTS, and (optionally) a friendly getter like the one below.
We are NOT pre-building settings we don't need yet — that's the project rule.

Settings live in:  ~/JobWatchData/settings.json   (a simple key -> value map)

Current settings:
    dormancy_days : int  — days with no check before a phase counts as dormant.
"""

import json
from pathlib import Path

from . import paths


# The single source of truth for default values. A setting that has never been
# changed simply isn't in the file, and we fall back to the default here. This
# is what makes the store safe to grow: a new key with a new default is one line.
DEFAULTS = {
    "dormancy_days": 21,
}

# Guard rails for values a person might set, so a typo can't break the logic.
# (Plain-language messages; this app's user is not a coder.)
MIN_DORMANCY_DAYS = 1
MAX_DORMANCY_DAYS = 365


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


def _validate_dormancy_days(value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise SettingsError(
            f"The dormancy setting needs to be a whole number of days, "
            f"not '{value}'."
        )
    if days < MIN_DORMANCY_DAYS or days > MAX_DORMANCY_DAYS:
        raise SettingsError(
            f"The dormancy setting should be between {MIN_DORMANCY_DAYS} and "
            f"{MAX_DORMANCY_DAYS} days. You gave {days}."
        )
    return days


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
