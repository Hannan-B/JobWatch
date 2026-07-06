"""
trends.py  (Phase B.5)
======================
Snapshots hold full job detail and get trimmed over time. Trends are the tiny,
cheap-to-keep counts we hold onto for ~a year so we can answer questions like:

    "Apple London finance roles went 2 -> 5 -> 9 across this phase."

For each snapshot we record one small row per (department, location) combo:
how many open roles there were, on that date, for that company, in that phase.
These rows are minuscule, so keeping a year of them costs almost nothing.

Where they live:  ~/JobWatchData/trends.json

The locked Trend entry shape (see DATA_FORMATS.md):
    {
        "date":            "2026-06-15",
        "company_key":     "apple",
        "phase_id":        "phase-2026-04-active",
        "department":      "Finance",
        "location_bucket": "London",
        "open_count":      9
    }

This module just COUNTS and APPENDS. The clever banding-by-phase charts are
Phase F; here we only make sure the raw counts exist to chart later.
"""

import json
import datetime
from collections import Counter
from pathlib import Path

from . import paths


def _trends_file() -> Path:
    return paths.data_root() / "trends.json"


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _load() -> dict:
    """Read the trend log, or return an empty one if it doesn't exist yet."""
    f = _trends_file()
    if not f.exists():
        return {"entries": []}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Trends are non-critical; if the file is unreadable, start fresh rather
        # than crash a job check. (Snapshots remain the source of truth.)
        return {"entries": []}
    if not isinstance(data, dict) or "entries" not in data:
        return {"entries": []}
    return data


def _save(data: dict) -> None:
    paths.ensure_data_dirs()
    _trends_file().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bucket_location(location: str) -> str:
    """
    Reduce a free-text location to a coarse bucket for trend grouping.
    Trends don't need pinpoint accuracy - they need stable, comparable buckets.
    A blank location becomes 'Unknown' so it still counts.

    This is deliberately simple: it takes the first comma-separated part and
    tidies whitespace, so "London, United Kingdom" and "London,United Kingdom"
    both bucket to "London". Country-only strings like "United Kingdom" bucket
    to themselves. (The app's real location FILTER is separate - Phase C.)
    """
    loc = (location or "").strip()
    if not loc:
        return "Unknown"
    first = loc.split(",")[0].strip()
    return first or "Unknown"


def _count_by_bucket(jobs: list) -> Counter:
    """Count a job list by (department, location_bucket) — the trend grouping."""
    counts = Counter()
    for j in (jobs or []):
        dept = (j.get("department") or "").strip()
        loc_bucket = _bucket_location(j.get("location", ""))
        counts[(dept, loc_bucket)] += 1
    return counts


def record_snapshot_trends(company_key: str, phase_id: str, jobs: list,
                           date: str | None = None,
                           new_jobs: list | None = None,
                           removed_jobs: list | None = None,
                           phase_type: str | None = None) -> int:
    """
    B.5 (+ Phase N deltas) - Count today's jobs by (department, location bucket)
    and append one small entry per combo to the trend log.

    Returns the number of trend entries added.

    Department is taken as-is from the job ("" stays "" -> shown as blank dept).
    Location is bucketed coarsely so counts group sensibly over time.

    Phase N — added/removed deltas (optional, backward-compatible):
        new_jobs     - the roles the run reported as NEW since the last check
        removed_jobs - the roles the run reported as REMOVED since the last check

    When given, each entry also carries `added_count` / `removed_count` for that
    same (department, location_bucket) cell, bucketed exactly like open_count so
    the three numbers line up on one chart. Both default to 0 (and to absent on
    older rows written before Phase N — readers must treat missing as 0). A
    baseline run passes new_jobs = the full list and removed_jobs = [] (everything
    is "added" on the first check of a phase), which the caller arranges.

    The deltas are bucketed independently of the open list, so a cell can show
    e.g. open_count 0 with removed_count 3 (a team that emptied out this check):
    we therefore iterate the UNION of all three count maps' keys, not just the
    open list's, so a removal in a now-empty bucket is never dropped.
    """
    date = date or _today_str()
    open_counts = _count_by_bucket(jobs)
    track_deltas = new_jobs is not None or removed_jobs is not None
    added_counts = _count_by_bucket(new_jobs) if track_deltas else Counter()
    removed_counts = _count_by_bucket(removed_jobs) if track_deltas else Counter()

    # Union of every (dept, loc) cell seen across open / added / removed, so a
    # bucket that only appears in removals (now zero-open) still records its loss.
    all_cells = set(open_counts) | set(added_counts) | set(removed_counts)

    data = _load()
    added = 0
    for (dept, loc_bucket) in sorted(all_cells):
        entry = {
            "date": date,
            "company_key": company_key,
            "phase_id": phase_id,
            "department": dept,
            "location_bucket": loc_bucket,
            "open_count": open_counts.get((dept, loc_bucket), 0),
        }
        # Post-Phase-O: stamp the phase's TYPE at write-time, so a single phase
        # that switched active<->casual can be split into active/casual segments
        # on the chart (the phase record only knows its *current* type). Optional
        # and backward-compatible: rows written without it simply don't carry the
        # field, and readers treat a missing phase_type as "unknown" (no split).
        if phase_type:
            entry["phase_type"] = phase_type
        # Only stamp delta fields when we were given delta data — keeps pre-N
        # behaviour byte-identical when the caller doesn't pass new/removed.
        if track_deltas:
            entry["added_count"] = added_counts.get((dept, loc_bucket), 0)
            entry["removed_count"] = removed_counts.get((dept, loc_bucket), 0)
        data["entries"].append(entry)
        added += 1
    _save(data)
    return added


def entries_for(company_key: str | None = None,
                phase_id: str | None = None) -> list:
    """
    Return trend entries, optionally filtered by company and/or phase.
    Handy for Phase F charts and for eyeballing that counts are landing.
    """
    out = []
    for e in _load().get("entries", []):
        if company_key is not None and e.get("company_key") != company_key:
            continue
        if phase_id is not None and e.get("phase_id") != phase_id:
            continue
        out.append(e)
    return out


# Quick manual test:  python3 -m jobwatch.trends
if __name__ == "__main__":
    sample = [
        {"id": "1", "title": "A", "location": "London, United Kingdom", "department": "Finance", "url": ""},
        {"id": "2", "title": "B", "location": "London,United Kingdom", "department": "Finance", "url": ""},
        {"id": "3", "title": "C", "location": "United Kingdom", "department": "", "url": ""},
    ]
    n = record_snapshot_trends("demo", "phase-demo", sample)
    print(f"Added {n} trend entries.")
    for e in entries_for("demo", "phase-demo"):
        print(f"  {e['date']} {e['department'] or '(none)'} / "
              f"{e['location_bucket']}: {e['open_count']}")
