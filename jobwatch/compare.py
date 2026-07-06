"""
compare.py  (Phase B.3 + B.4)
=============================
The heart of JobWatch: given two snapshots, work out what CHANGED.

We never guess what's new. We compute it, exactly, by job id:
    NEW       = in today's list, not in last time's
    REMOVED   = in last time's list, not in today's
    UNCHANGED = in both

Each job carries a stable 'id' from its board, so this comparison is precise.
If there is NO previous snapshot (the first check in a phase), that's the
BASELINE: there's nothing to compare against, so everything counts as new and
nothing as removed. That's not an error - it's the honest starting point.

This module does pure logic only: no files, no network. You hand it two job
lists (or two snapshot dicts) and it hands back the differences. That keeps it
easy to trust and easy to test.
"""


def _index_by_id(jobs: list) -> dict:
    """
    Build a lookup of id -> job for a list of jobs.
    Ids are treated as opaque strings (per DATA_FORMATS rules). Jobs with a
    blank id are skipped from matching, because a blank id can't identify a
    role reliably - counting them would create phantom 'new/removed' churn.
    """
    out = {}
    for j in jobs:
        jid = str(j.get("id", "")).strip()
        if jid:
            out[jid] = j
    return out


def compare_jobs(previous_jobs: list | None, current_jobs: list) -> dict:
    """
    B.3 + B.4 - Compare two job lists by id.

    previous_jobs - last time's jobs, or None/[] if this is the first check
    current_jobs  - this check's jobs

    Returns a dict:
        {
            "new":        [job, ...],   # appeared since last time
            "removed":    [job, ...],   # gone since last time
            "unchanged":  [job, ...],   # present both times (today's version)
            "is_baseline": bool,        # True when there was no previous snapshot
            "counts": {"new": n, "removed": n, "unchanged": n, "total": n},
        }

    The baseline case (no previous) returns every current job as "new",
    nothing removed - the honest "first look" result.
    """
    current_jobs = current_jobs or []
    is_baseline = previous_jobs is None

    cur_idx = _index_by_id(current_jobs)

    if is_baseline:
        new = list(cur_idx.values())
        result = {
            "new": new,
            "removed": [],
            "unchanged": [],
            "is_baseline": True,
        }
    else:
        prev_idx = _index_by_id(previous_jobs or [])
        new = [job for jid, job in cur_idx.items() if jid not in prev_idx]
        removed = [job for jid, job in prev_idx.items() if jid not in cur_idx]
        unchanged = [job for jid, job in cur_idx.items() if jid in prev_idx]
        result = {
            "new": new,
            "removed": removed,
            "unchanged": unchanged,
            "is_baseline": False,
        }

    result["counts"] = {
        "new": len(result["new"]),
        "removed": len(result["removed"]),
        "unchanged": len(result["unchanged"]),
        "total": len(current_jobs),
    }
    return result


def compare_snapshots(previous_snapshot: dict | None,
                      current_snapshot: dict) -> dict:
    """
    Convenience wrapper that takes whole snapshot dicts instead of bare job
    lists. Refuses to compare across different phases, enforcing the locked
    rule right where a mistake would matter.
    """
    if previous_snapshot is not None:
        prev_phase = previous_snapshot.get("phase_id")
        cur_phase = current_snapshot.get("phase_id")
        if prev_phase != cur_phase:
            raise ValueError(
                "Refusing to compare snapshots from different phases "
                f"('{prev_phase}' vs '{cur_phase}'). Comparison is always "
                "within a single phase."
            )
    prev_jobs = previous_snapshot.get("jobs") if previous_snapshot else None
    return compare_jobs(prev_jobs, current_snapshot.get("jobs", []))


def summary_line(result: dict) -> str:
    """A short plain-language summary, handy for printing to the user."""
    c = result["counts"]
    if result.get("is_baseline"):
        return (f"First check this phase: {c['total']} roles recorded as the "
                f"baseline (all counted as new).")
    return (f"{c['new']} new, {c['removed']} removed, "
            f"{c['unchanged']} unchanged ({c['total']} live now).")


# Quick manual test:  python3 -m jobwatch.compare
if __name__ == "__main__":
    before = [
        {"id": "1", "title": "Keep Me", "location": "London", "department": "", "url": ""},
        {"id": "2", "title": "Remove Me", "location": "London", "department": "", "url": ""},
    ]
    after = [
        {"id": "1", "title": "Keep Me", "location": "London", "department": "", "url": ""},
        {"id": "3", "title": "Brand New", "location": "London", "department": "", "url": ""},
    ]
    print("Baseline:", summary_line(compare_jobs(None, after)))
    print("Normal:  ", summary_line(compare_jobs(before, after)))
