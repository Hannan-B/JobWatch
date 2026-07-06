"""
storage.py  (Phase B.1 + B.2)
=============================
This is the snapshot engine: how JobWatch saves a dated list of one company's
jobs, and how it finds the previous one to compare against.

A "snapshot" is the atom of the whole system: one file = one check of one
company at one moment. We never edit a snapshot after writing it; history is
just the pile of snapshots in date order.

Where snapshots live:
    ~/JobWatchData/snapshots/<company-key>/<timestamp>.json

The timestamp in the filename is what makes "most recent" a simple sort, and
keeps every check as its own permanent record.

The locked Snapshot shape (see DATA_FORMATS.md):
    {
        "company_key": "ogilvy-uk",
        "phase_id":    "phase-2026-04-active",
        "taken_at":    "2026-06-15T09:30:00",
        "jobs":        [ {id,title,location,department,url}, ... ]
    }

Golden rule honored here: we only ever look for a "previous" snapshot that has
the SAME phase_id. Snapshots from an older phase are never returned as the prior
one, which is what keeps comparison clean across job-hunt gaps.
"""

import json
import datetime
from pathlib import Path

from . import paths


def _snapshots_root() -> Path:
    """The folder holding every company's snapshot sub-folder."""
    return paths.data_root() / "snapshots"


def _company_dir(company_key: str) -> Path:
    """The sub-folder for one company's snapshots."""
    return _snapshots_root() / company_key


def _timestamp_now() -> str:
    """
    An ISO-style timestamp with microseconds, e.g. '2026-06-15T09:30:00.123456'.

    Why microseconds and not whole seconds? Two snapshots written in the same
    second must never get the SAME timestamp - if they did, we couldn't tell
    which came first, and 'find the previous one' would break. Microseconds make
    every snapshot's time unique in practice. (DATA_FORMATS shows whole seconds
    for readability; the extra precision is backward-compatible - it's still a
    sortable ISO string.)
    """
    return datetime.datetime.now().isoformat()


def _safe_filename(timestamp: str) -> str:
    """
    Turn a timestamp into a filename that's valid on a Mac. ISO timestamps
    contain ':' which is awkward in filenames, so we swap ':' for '-'. The
    file still sorts correctly because the date/time order is preserved.
    """
    return timestamp.replace(":", "-") + ".json"


def write_snapshot(company_key: str, phase_id: str, jobs: list,
                   taken_at: str | None = None) -> Path:
    """
    B.1 - Save a snapshot of one company's current jobs.

    company_key - which company (matches the 'key' in companies.json)
    phase_id    - which phase this check belongs to (REQUIRED; drives comparison)
    jobs        - the clean job list from fetch_jobs()
    taken_at    - optional ISO timestamp; defaults to now

    Returns the path of the file written. Never overwrites an existing check:
    if two snapshots somehow share a timestamp, a counter is added so no
    history is ever lost.
    """
    if not company_key or not str(company_key).strip():
        raise ValueError("write_snapshot needs a company_key.")
    if not phase_id or not str(phase_id).strip():
        # This guards the locked rule: every snapshot must belong to a phase.
        raise ValueError(
            "write_snapshot needs a phase_id - every snapshot must belong to a "
            "phase so comparisons stay within one phase."
        )

    paths.ensure_data_dirs()
    taken_at = taken_at or _timestamp_now()

    snapshot = {
        "company_key": company_key,
        "phase_id": phase_id,
        "taken_at": taken_at,
        "jobs": jobs,
    }

    cdir = _company_dir(company_key)
    cdir.mkdir(parents=True, exist_ok=True)

    target = cdir / _safe_filename(taken_at)
    # Don't clobber an existing file with the same timestamp - keep all history.
    if target.exists():
        stem = target.stem
        n = 2
        while (cdir / f"{stem}--{n}.json").exists():
            n += 1
        target = cdir / f"{stem}--{n}.json"

    target.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return target


def _read_snapshot(path: Path) -> dict:
    """Load one snapshot file into a dict."""
    return json.loads(path.read_text(encoding="utf-8"))


def list_snapshots(company_key: str) -> list:
    """
    Return all snapshot file paths for a company, OLDEST first.

    Primary sort is the timestamp in the filename (chronological). As a
    tie-breaker - for the rare case where two files share a timestamp - we fall
    back to the file's modification time, so write order is still respected and
    'latest' / 'previous' never get confused.
    """
    cdir = _company_dir(company_key)
    if not cdir.exists():
        return []

    def sort_key(p: Path):
        # Filename without the ".json" / "--N" suffix sorts by timestamp;
        # mtime breaks any tie in true write order.
        return (p.name, p.stat().st_mtime)

    return sorted(cdir.glob("*.json"), key=sort_key)


def load_latest_snapshot(company_key: str,
                         phase_id: str | None = None) -> dict | None:
    """
    Return the most recent snapshot for a company as a dict, or None.
    If phase_id is given, only snapshots from THAT phase are considered.
    """
    files = list_snapshots(company_key)
    for path in reversed(files):  # newest first
        snap = _read_snapshot(path)
        if phase_id is None or snap.get("phase_id") == phase_id:
            return snap
    return None


def load_previous_snapshot(company_key: str, phase_id: str,
                           before_taken_at: str | None = None) -> dict | None:
    """
    B.2 - Find the snapshot to COMPARE AGAINST: the most recent prior snapshot
    for this company WITHIN THE SAME PHASE.

    company_key     - the company
    phase_id        - only snapshots from this phase are eligible (locked rule)
    before_taken_at - optional; only consider snapshots taken strictly before
                      this timestamp. Use this when you've just written today's
                      snapshot and want "the one before it". If omitted, returns
                      the latest snapshot in the phase.

    Returns the prior snapshot dict, or None if there isn't one yet (which is
    the baseline case - see compare.py B.4).
    """
    files = list_snapshots(company_key)
    candidates = []
    for path in files:
        snap = _read_snapshot(path)
        if snap.get("phase_id") != phase_id:
            continue
        if before_taken_at is not None and snap.get("taken_at", "") >= before_taken_at:
            continue
        candidates.append(snap)
    if not candidates:
        return None
    # Most recent of the eligible ones.
    return sorted(candidates, key=lambda s: s.get("taken_at", ""))[-1]


# Quick manual test:  python3 -m jobwatch.storage
if __name__ == "__main__":
    demo_jobs = [
        {"id": "1", "title": "Test Role A", "location": "London",
         "department": "Eng", "url": "http://example.com/1"},
    ]
    p = write_snapshot("demo-company", "phase-demo", demo_jobs)
    print("Wrote snapshot:", p)
    latest = load_latest_snapshot("demo-company", "phase-demo")
    print("Loaded latest, job count:", len(latest["jobs"]) if latest else 0)
    prev = load_previous_snapshot("demo-company", "phase-demo",
                                  before_taken_at=latest["taken_at"])
    print("Previous before latest:", prev)
