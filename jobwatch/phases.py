"""
phases.py  (Phase B core + Phase D lifecycle)
=============================================================
A "phase" is a named stretch of your job search. Everything JobWatch records is
stamped with the phase it happened in, because the golden comparison rule is:

    we only ever compare two snapshots from the SAME phase.

That rule is what lets you wander off for six months and come back to a clean
slate without losing your old history: you just start a new phase.

Phase B built the small core: create a phase, list phases, tell you which one
is "current". Phase D adds the lifecycle on top of those same records (nothing
from Phase B was thrown away):

    - end the current phase
    - start a new phase that cleanly ends the previous one first, so there is
      only ever ONE open phase (the old code could leave two open and then
      guess between them — that gap is closed here)
    - switch a phase's type (active <-> casual) without ending it
    - report a phase's CADENCE state (due / overdue) for the gentle active-phase
      nudge — distinct from dormancy, which is the longer "been away" reset

Phases live in:  ~/JobWatchData/phases.json   (a list of phase records)

The locked Phase shape (see DATA_FORMATS.md):
    {
        "id":           "phase-2026-04-active",   # stable unique string
        "name":         "Active hunt - April 2026",
        "type":         "active"  or  "casual",
        "cadence_days": 3,                          # expected gap between checks
        "started_on":   "2026-04-01",
        "ended_on":     null                        # null = this is the current phase
    }
"""

import json
import datetime
from pathlib import Path

from . import paths


# Where the phase list lives, at the root of your data folder.
def _phases_file() -> Path:
    return paths.data_root() / "phases.json"


# Sensible default cadences (days between checks) per phase type.
DEFAULT_CADENCE = {
    "active": 3,    # real hunt: check every couple of days
    "casual": 10,   # market watch: check every week or two
}

VALID_TYPES = ("active", "casual")


class PhaseError(Exception):
    """Raised for phase problems, with a plain-language message safe to show."""


def _today_str() -> str:
    """Today's date as YYYY-MM-DD."""
    return datetime.date.today().isoformat()


def _load_all() -> list:
    """Read every phase record. Returns [] if the file doesn't exist yet."""
    f = _phases_file()
    if not f.exists():
        return []
    try:
        text = f.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        raise PhaseError(
            "Couldn't read your phases file. It may be corrupted. "
            f"(File: {f})"
        ) from e
    if not isinstance(data, list):
        raise PhaseError("The phases file isn't in the expected list format.")
    return data


def _save_all(phases: list) -> None:
    """Write the full phase list back to disk (creates the folder if needed)."""
    paths.ensure_data_dirs()  # guarantees the data folder exists & is outside git
    f = _phases_file()
    f.write_text(json.dumps(phases, indent=2), encoding="utf-8")


def _make_phase_id(name: str, type_: str, started_on: str) -> str:
    """
    Build a stable, human-readable id like 'phase-2026-04-active'.
    Uses the start year-month plus the type. If that id already exists
    (e.g. two active phases started the same month), add a -2, -3 suffix.
    """
    year_month = started_on[:7]  # "2026-04"
    base = f"phase-{year_month}-{type_}"
    existing = {p.get("id") for p in _load_all()}
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def list_phases() -> list:
    """Return all phase records, oldest-first by start date."""
    return sorted(_load_all(), key=lambda p: p.get("started_on", ""))


def current_phase() -> dict | None:
    """
    Return the current phase (the one with ended_on == null), or None if there
    isn't one. Only one phase should ever be current at a time; if somehow more
    than one is open, we return the most recently started, the safest guess.
    """
    open_phases = [p for p in _load_all() if p.get("ended_on") in (None, "")]
    if not open_phases:
        return None
    return sorted(open_phases, key=lambda p: p.get("started_on", ""))[-1]


def get_phase(phase_id: str) -> dict | None:
    """Return one phase record by id, or None if not found."""
    for p in _load_all():
        if p.get("id") == phase_id:
            return p
    return None


def create_phase(name: str,
                 type_: str = "active",
                 cadence_days: int | None = None,
                 started_on: str | None = None) -> dict:
    """
    Create and save a new phase, and make it the current one.

    name         - what you'll call this stretch, e.g. "Active hunt - April 2026"
    type_        - "active" (real hunt) or "casual" (market watch)
    cadence_days - expected gap between checks; defaults sensibly by type
    started_on   - YYYY-MM-DD; defaults to today

    Single-open-phase guarantee (Phase D): if another phase is currently open,
    it is ENDED first (its ended_on is set to this phase's start date), so there
    is only ever one open phase. This closes the Phase B gap where two phases
    could be open at once and current_phase() had to guess between them. Old
    data is untouched — ending a phase is just stamping an end date; nothing is
    deleted, and the ended phase stays fully browsable.
    """
    name = (name or "").strip()
    if not name:
        raise PhaseError("A phase needs a name.")
    if type_ not in VALID_TYPES:
        raise PhaseError(
            f"Phase type must be one of {VALID_TYPES}, not '{type_}'."
        )
    started_on = started_on or _today_str()
    if cadence_days is None:
        cadence_days = DEFAULT_CADENCE[type_]

    phases = _load_all()

    # End any currently-open phase first (single-open-phase guarantee).
    # We end it the day the new phase starts — clean, non-destructive.
    for p in phases:
        if p.get("ended_on") in (None, ""):
            p["ended_on"] = started_on

    phase = {
        "id": _make_phase_id(name, type_, started_on),
        "name": name,
        "type": type_,
        "cadence_days": int(cadence_days),
        "started_on": started_on,
        "ended_on": None,
    }

    phases.append(phase)
    _save_all(phases)
    return phase


def ensure_current_phase(default_name: str = "Getting started",
                         type_: str = "active") -> dict:
    """
    Convenience used by the rest of the app: return the current phase, creating
    a first one if none exists yet. This means snapshots always have a real
    phase to belong to, satisfying the locked 'same phase' comparison rule
    without requiring the full Phase D UX to exist first.
    """
    cur = current_phase()
    if cur is not None:
        return cur
    return create_phase(default_name, type_=type_)


# ---------------------------------------------------------------------------
# Phase D lifecycle: end, switch, cadence state
# ---------------------------------------------------------------------------

def _save_one(updated: dict) -> dict:
    """Write a single changed phase back into the list (matched by id)."""
    phases = _load_all()
    for i, p in enumerate(phases):
        if p.get("id") == updated.get("id"):
            phases[i] = updated
            _save_all(phases)
            return updated
    raise PhaseError(f"Couldn't find a phase with id '{updated.get('id')}' to save.")


def end_phase(phase_id: str | None = None, ended_on: str | None = None) -> dict:
    """
    End a phase by stamping its end date. With no phase_id, ends the current
    (open) phase. This is non-destructive: the phase and all its snapshots stay
    on disk and browsable; ending only means "no longer the current phase".

    After ending with no replacement, there is NO current phase — the app is
    dormant/quiet until a new phase is started. That's the intended resting
    state between hunts.
    """
    ended_on = ended_on or _today_str()
    if phase_id is None:
        cur = current_phase()
        if cur is None:
            raise PhaseError("There's no current phase to end.")
        target = cur
    else:
        target = get_phase(phase_id)
        if target is None:
            raise PhaseError(f"No phase found with id '{phase_id}'.")
        if target.get("ended_on") not in (None, ""):
            raise PhaseError(f"Phase '{phase_id}' has already ended.")

    target = dict(target)
    target["ended_on"] = ended_on
    return _save_one(target)


def delete_phase(phase_id: str) -> dict:
    """
    DELETE a phase record outright (Post-Phase-O: History cleanup).

    Unlike end_phase, this is DESTRUCTIVE: the phase is removed from phases.json
    entirely. It exists so test phases (and any past phase you no longer want)
    can be cleared from History. The caller is responsible for wiping the phase's
    OWN DATA (snapshots, trends, applications, saved rows) — this function only
    touches the phase list. (The server's delete endpoint does both, in order:
    wipe data first, then remove the record, so a half-delete never strands an
    orphaned phase pointer.)

    The current phase MAY be deleted (the user asked for this): deleting the
    open phase simply leaves NO current phase, i.e. the app drops cleanly into
    its dormant resting state — the same end state as ending it, but with the
    record gone rather than stamped closed. Because only one phase is ever open,
    removing it can't strand a second pointer.

    Returns the deleted phase record (so the caller can report what went).
    Raises PhaseError if no phase has that id.
    """
    phase_id = (phase_id or "").strip()
    if not phase_id:
        raise PhaseError("Which phase should I delete?")
    phases = _load_all()
    removed = None
    kept = []
    for p in phases:
        if p.get("id") == phase_id and removed is None:
            removed = p
        else:
            kept.append(p)
    if removed is None:
        raise PhaseError(f"No phase found with id '{phase_id}'.")
    _save_all(kept)
    return removed


def rename_phase(new_name: str, phase_id: str | None = None) -> dict:
    """
    Rename a phase WITHOUT changing anything else (Post-Phase-O: continuation
    rename). With no phase_id, renames the current phase. Only the display name
    moves — the phase id, type, dates, and all its data stay exactly as they
    were, so comparison history and trends are untouched. This is the "keep the
    same phase, just relabel it" half of the switch-type continuation flow.
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise PhaseError("A phase needs a name.")
    if phase_id is None:
        target = current_phase()
        if target is None:
            raise PhaseError("There's no current phase to rename.")
    else:
        target = get_phase(phase_id)
        if target is None:
            raise PhaseError(f"No phase found with id '{phase_id}'.")
    target = dict(target)
    target["name"] = new_name
    return _save_one(target)


def switch_type(new_type: str, phase_id: str | None = None,
                update_cadence: bool = True) -> dict:
    """
    Switch a phase between 'active' and 'casual' WITHOUT ending it. With no
    phase_id, switches the current phase.

    This is the warm casual<->active change: because the phase isn't ending,
    comparison history is preserved (the orchestrator keeps comparing against
    the last check). Only dormancy resets the baseline, never a type switch.

    update_cadence - if True (default), also move cadence_days to the new type's
                     sensible default. If the user had hand-set a custom cadence
                     they want to keep, pass False.
    """
    if new_type not in VALID_TYPES:
        raise PhaseError(
            f"Phase type must be one of {VALID_TYPES}, not '{new_type}'."
        )
    if phase_id is None:
        target = current_phase()
        if target is None:
            raise PhaseError("There's no current phase to switch.")
    else:
        target = get_phase(phase_id)
        if target is None:
            raise PhaseError(f"No phase found with id '{phase_id}'.")

    target = dict(target)
    if target.get("type") == new_type:
        return target  # already that type; nothing to do

    target["type"] = new_type
    if update_cadence:
        target["cadence_days"] = DEFAULT_CADENCE[new_type]
    return _save_one(target)


def cadence_state(last_check_date: str | None,
                  phase: dict | None = None,
                  today: datetime.date | None = None) -> dict:
    """
    Is the current phase DUE or OVERDUE for a check, based on its cadence?

    This is the gentle in-phase nudge (active phases expect a check every couple
    of days; casual every week or two). It is DISTINCT from dormancy:
        - "overdue" is a soft nudge WITHIN a live, warm phase.
        - "dormant" (see dormancy.py) is the much longer lapse that resets the
          baseline. A phase can be overdue (e.g. 5 days into a 3-day cadence)
          while nowhere near dormant (21 days).

    Returns a small dict with the state and a plain-language message. States:
        "fresh"   - checked recently, within cadence.
        "due"     - at/just past the cadence; a good time to check.
        "overdue" - well past cadence (more than ~2x), a stronger nudge.
        "new"     - phase never checked yet.
    """
    phase = phase or current_phase()
    if phase is None:
        return {"state": "none", "message": "There's no current phase."}

    cadence = int(phase.get("cadence_days") or DEFAULT_CADENCE.get(
        phase.get("type", "active"), 3))
    p_type = phase.get("type", "active")

    if last_check_date in (None, ""):
        return {
            "state": "new",
            "days_since": None,
            "cadence_days": cadence,
            "message": "This phase hasn't been checked yet.",
        }

    today = today or datetime.date.today()
    gap = max(0, (today - datetime.date.fromisoformat(last_check_date)).days)

    if gap < cadence:
        state = "fresh"
        message = f"Checked {gap} day{'s' if gap != 1 else ''} ago — within the "\
                  f"{cadence}-day rhythm."
    elif gap < cadence * 2:
        state = "due"
        message = f"It's been {gap} days — about time for a check "\
                  f"({cadence}-day rhythm)."
    else:
        state = "overdue"
        # Casual phases deliberately don't nag; soften the wording.
        if p_type == "casual":
            message = f"It's been {gap} days. No rush in a casual phase, but "\
                      f"you could check when you fancy."
        else:
            message = f"It's been {gap} days — a bit overdue for this active "\
                      f"phase's {cadence}-day rhythm."
    return {
        "state": state,
        "days_since": gap,
        "cadence_days": cadence,
        "phase_type": p_type,
        "message": message,
    }


# Quick manual test:  python3 -m jobwatch.phases
if __name__ == "__main__":
    print("Current phase:", current_phase())
    p = ensure_current_phase()
    print("Ensured current phase:")
    print(json.dumps(p, indent=2))
    print("\nAll phases:")
    for ph in list_phases():
        print(f"  - {ph['id']}  ({ph['type']}, started {ph['started_on']}, "
              f"ended {ph['ended_on']})")
