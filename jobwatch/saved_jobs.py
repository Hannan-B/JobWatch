"""
saved_jobs.py  (Phase K — the "saved / favourite roles" store)
==============================================================
This is where JobWatch remembers the roles you've STARRED while reading a run.
It's the small companion to the run engine: starring a role on the Jobs page
drops a copy of it here; the Saved Jobs tab reads it back; "Applied" marks it;
"Remove" deletes it.

Everything here reads and writes one file:
    ~/JobWatchData/saved_jobs.json

It follows the same sibling-data-file pattern the rest of v1/v2 uses (external
data folder, created via paths.ensure_data_dirs(), forgiving reads — a missing
or corrupt file just yields an empty list, because saved jobs are convenience
data, never the system of record). It deliberately does NOT touch the engine's
snapshot / compare / trends logic.

The locked Saved-Job shape:
    {
        "id":           "4673971005",          # the role id (opaque string)
        "company_key":  "ogilvy-uk",
        "company_name": "Ogilvy UK",
        "title":        "Social Strategy Director",
        "location":     "London, United Kingdom",
        "department":   "PR, Influence and Social",
        "url":          "https://.../jobs/4673971005",
        "phase_id":     "phase-2026-04-active", # the phase it was saved in
        "saved_on":     "2026-06-21T14:05:00",  # ISO timestamp (drives date sort)
        "applied":      false,                  # set true by mark_applied()
        "applied_on":   null                    # ISO timestamp when applied
    }

Identity: a saved job is uniquely identified by (company_key, id). Starring the
same role twice is a no-op (idempotent); un-starring removes it.

DORMANCY RESET (locked with the user): saved jobs are scoped to an ACTIVE hunt.
When a phase lapses into dormancy (or there's no current phase at all), the
saved list is wiped — a clean slate for the next hunt, the same spirit as the
snapshot clean-baseline. We enforce this lazily, ON READ: list_saved() asks the
caller-supplied "is it dormant right now?" check and, if so, clears the file
before returning []. Doing it on read means we never need a reliable "the moment
dormancy began" event hook — any read after dormancy sets in returns empty and
tidies the file. Applied-but-not-yet-in-the-tracker jobs are cleared too (the
user chose a full reset); once Phase L exists, applied roles will already live
in the tracker, so nothing is lost then.
"""

import json
import datetime
from pathlib import Path

from . import paths


def _saved_file() -> Path:
    return paths.data_root() / "saved_jobs.json"


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_raw() -> list:
    """Read the saved-jobs list. Forgiving: missing or unreadable file -> [].
    A non-list payload (corrupt) -> [] as well (convenience data, never fatal)."""
    f = _saved_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    # Defensive: keep only dict entries that at least have an id + company_key.
    out = []
    for r in data:
        if isinstance(r, dict) and r.get("id") and r.get("company_key"):
            out.append(r)
    return out


def _save_raw(records: list) -> None:
    paths.ensure_data_dirs()
    _saved_file().write_text(json.dumps(records, indent=2), encoding="utf-8")


def clear_all() -> int:
    """Wipe the saved list entirely. Returns how many records were removed.
    Used by the dormancy reset and available for a manual 'clear all' later."""
    existing = _load_raw()
    n = len(existing)
    if n:
        _save_raw([])
    return n


def _same(record: dict, company_key: str, job_id: str) -> bool:
    return (record.get("company_key") == company_key
            and str(record.get("id")) == str(job_id))


def list_saved(is_dormant: bool = False) -> list:
    """
    Return all saved jobs, newest-save first.

    is_dormant - when True, the saved list is considered expired (the phase has
                 lapsed, or there's no current phase): we clear the file and
                 return []. This is the locked dormancy-reset behaviour, enforced
                 lazily on read so no event hook is needed. The caller (server)
                 computes dormancy from the same dormancy.py the rest of the app
                 uses, and passes the boolean in.

    Records are returned as stored; callers sort/filter for display (by date,
    company, or alphabetical — see the Jobs page).
    """
    if is_dormant:
        clear_all()
        return []
    records = _load_raw()
    # Newest save first (stable, by saved_on then insertion order).
    records.sort(key=lambda r: r.get("saved_on", ""), reverse=True)
    return records


def is_saved(company_key: str, job_id: str) -> bool:
    """True if this exact role is already starred."""
    return any(_same(r, company_key, job_id) for r in _load_raw())


def add(job: dict, company_key: str, company_name: str,
        phase_id: str | None) -> dict:
    """
    Star a role. Idempotent: if it's already saved, the existing record is
    returned unchanged (we never duplicate, and we never silently un-apply an
    already-applied save).

    job - a job dict as it appears in a run result / current-roles list, i.e.
          {id, title, location, department, url, ...}. Only the stable display
          fields are copied; flags like interest_rank are not persisted (they're
          recomputed from interests whenever roles are shown).
    """
    job_id = str(job.get("id") or "").strip()
    company_key = (company_key or "").strip()
    if not job_id or not company_key:
        raise ValueError("A saved job needs a role id and a company key.")

    records = _load_raw()
    for r in records:
        if _same(r, company_key, job_id):
            return r  # already saved — idempotent

    record = {
        "id": job_id,
        "company_key": company_key,
        "company_name": (company_name or company_key).strip(),
        "title": job.get("title") or "(untitled role)",
        "location": job.get("location") or "",
        "department": job.get("department") or "",
        "url": job.get("url") or "",
        "phase_id": phase_id,
        "saved_on": _now_iso(),
        "applied": False,
        "applied_on": None,
    }
    records.append(record)
    _save_raw(records)
    return record


def remove(company_key: str, job_id: str) -> bool:
    """Un-star a role. Returns True if one was removed, False if it wasn't there."""
    records = _load_raw()
    remaining = [r for r in records if not _same(r, company_key, job_id)]
    if len(remaining) == len(records):
        return False
    _save_raw(remaining)
    return True


def mark_applied(company_key: str, job_id: str) -> dict | None:
    """
    Mark a saved role as applied. The user's flow: from the Saved tab, pressing
    "Applied" flags the role and it leaves the ACTIVE saved view, on its way to
    the Application Tracker (Phase L).

    Until Phase L exists, we keep the record here with applied=True (so nothing
    is lost and the Saved tab can simply hide applied ones from its active list).
    When L is built, it will read these applied records to seed the tracker, then
    they can be dropped from here. Returns the updated record, or None if the role
    wasn't saved.
    """
    records = _load_raw()
    hit = None
    for r in records:
        if _same(r, company_key, job_id):
            r["applied"] = True
            r["applied_on"] = _now_iso()
            hit = r
            break
    if hit is not None:
        _save_raw(records)
    return hit


def list_applied() -> list:
    """All saved roles already marked applied. This is the hand-off list Phase L
    will adopt into the Application Tracker. Read-only convenience."""
    return [r for r in _load_raw() if r.get("applied")]


# Quick manual test:  python3 -m jobwatch.saved_jobs
if __name__ == "__main__":
    demo = {"id": "1", "title": "Test Role", "location": "London",
            "department": "Eng", "url": "http://example.com/1"}
    print("Starting saved count:", len(list_saved()))
    add(demo, "demo-co", "Demo Co", "phase-demo")
    add(demo, "demo-co", "Demo Co", "phase-demo")  # idempotent
    print("After saving once (twice attempted):", len(list_saved()))
    print("is_saved:", is_saved("demo-co", "1"))
    mark_applied("demo-co", "1")
    print("applied list:", [r["id"] for r in list_applied()])
    remove("demo-co", "1")
    print("After remove:", len(list_saved()))
