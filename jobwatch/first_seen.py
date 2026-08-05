"""
first_seen.py  (2026-08-05 — when did JobWatch first see this role?)
====================================================================
A small running index answering one question per role:

    Since when has this job been continuously present in my checks?

Everything here reads and writes one file:
    ~/JobWatchData/first_seen.json

WHY AN INDEX AND NOT A SCAN
---------------------------
The answer is derivable from the snapshot tree — the earliest snapshot holding a
job id IS its first sighting. But the tree runs to thousands of files and grows
with every check, and the screens that need this re-render in place on every
filter chip, sort and drag (the S32 discipline). A scan behind each click would
get slower every week until it had to be rebuilt as an index anyway. So: derive
once via rebuild_from_snapshots(), then maintain incrementally at the one point
in a run that already has the information (orchestrator._record_first_seen).

Same shape and same rules as the Phase N trends recorder: cheap, layered on top,
and NON-CRITICAL. Snapshots remain the source of truth. If this file is lost,
corrupt, or never written, rebuild_from_snapshots() regenerates it from scratch
and nothing else in the app is affected.

WHAT "FIRST SEEN" MEANS HERE — read this before trusting a date
---------------------------------------------------------------
It is NOT the date the employer posted the role. It is the start of the job's
current unbroken run of sightings in YOUR checks, which differs from the true
posting date in three ways worth knowing:

  * Snapshots store the SHOWN (location-filtered) list, not the raw fetch. A role
    that existed for weeks outside your filter has no record until the day it
    matched — or the day you widened the filter.
  * Adding a company backdates nothing. Every role already live when a company
    was added reads as first seen on that first check. Those entries carry
    bounded=True: the real date is "on or before" this one.
  * The clock RESTARTS on re-appearance (the owner's locked choice, 2026-08-05).
    If a job id leaves the snapshots and later returns, `since` moves to the
    return. Note the common causes of a gap are a filter change or the pre-fix
    Avature page drift, not a genuine re-listing.

The board's own posting date is the honest answer to "how long has this been on
the market", and is deferred to its own build. When it lands it becomes a
SEPARATE field (`posted_on`) — do not overload `since` with it. That separation
is why this field is called first_seen and not date_added.

THE RECORD SHAPE (per company, per job id)
    {
        "since":     "2026-07-18",   # start of the current unbroken run
        "last_seen": "2026-08-05",   # last check that included it
        "bounded":   false           # true = "on or before" (first-ever check)
    }
"""

import json
import datetime
from pathlib import Path

from . import paths
from . import storage


SCHEMA_VERSION = 1


def _index_file() -> Path:
    return paths.data_root() / "first_seen.json"


def _today_str(today: datetime.date | None = None) -> str:
    return (today or datetime.date.today()).isoformat()


# ---- Load / save (forgiving, like trends.py and saved_jobs.py) -------------

def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "companies": {}}


def load() -> dict:
    """Read the whole index. Forgiving: missing/unreadable/corrupt -> empty.

    A corrupt index must never stop the app — worst case the dates read as
    "not recorded" until rebuild_from_snapshots() is run."""
    f = _index_file()
    if not f.exists():
        return _empty()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("companies"), dict):
        return _empty()
    return data


def _save(data: dict) -> None:
    paths.ensure_data_dirs()
    _index_file().write_text(json.dumps(data, indent=2), encoding="utf-8")


def for_company(company_key: str, data: dict | None = None) -> dict:
    """The {job_id: record} map for one company ({} if unknown).

    Defensive at every level: `data.get("companies", {})` is NOT enough, because
    a corrupt index can carry the key with a null value and the default never
    fires. A bad index must degrade to "not recorded", never raise — this store
    is non-critical and must not be able to break a screen."""
    data = data if data is not None else load()
    if not isinstance(data, dict):
        return {}
    comps = data.get("companies")
    if not isinstance(comps, dict):
        return {}
    entry = comps.get(company_key)
    return entry if isinstance(entry, dict) else {}


# ---- Maintenance: called once per company per run --------------------------

def record_check(company_key: str,
                 current_jobs: list,
                 previous_job_ids=None,
                 today: datetime.date | None = None,
                 is_first_check: bool = False,
                 carry_through_gap: bool = False,
                 data: dict | None = None) -> dict:
    """
    Fold one company's check into the index and return the updated index.

    current_jobs      - the SHOWN job list just snapshotted for this company.
    previous_job_ids  - ids present in the immediately-preceding check, used to
                        tell "still here" from "came back". None means unknown.
    is_first_check    - True when this is the company's first-ever snapshot, so
                        every job's date is an upper bound (bounded=True).
    carry_through_gap - True on a dormant clean-baseline. The phase lapsed, so
                        WE stopped looking; the job didn't necessarily go away.
                        Existing entries keep their `since` rather than treating
                        the gap as a re-appearance. (Locked 2026-08-05: a dormant
                        gap must not restart a job's clock.)

    Does NOT write — the caller persists. Returns the mutated index dict.
    """
    data = data if data is not None else load()
    data.setdefault("companies", {})
    bucket = data["companies"].setdefault(company_key, {})
    stamp = _today_str(today)

    prev_ids = None if previous_job_ids is None else {str(i) for i in previous_job_ids}
    seen_now = set()

    for job in current_jobs or []:
        jid = job.get("id")
        if jid is None or jid == "":     # falsy-id rule: "0" is a valid id
            continue
        jid = str(jid)
        seen_now.add(jid)
        existing = bucket.get(jid)

        if not isinstance(existing, dict):
            # Never seen before: today is its start. Bounded when this is the
            # company's first-ever check (it may have been live long before).
            bucket[jid] = {"since": stamp, "last_seen": stamp,
                           "bounded": bool(is_first_check)}
            continue

        # Known job. Was it here last check? If we can't tell (prev_ids None),
        # assume continuity — never invent a gap we have no evidence for.
        contiguous = True
        if prev_ids is not None and not carry_through_gap:
            contiguous = jid in prev_ids

        if not contiguous:
            # It left and came back: the clock restarts (owner's locked rule).
            existing["since"] = stamp
            existing["bounded"] = False
        existing["last_seen"] = stamp

    return data


def record_check_and_save(company_key: str, current_jobs: list, **kwargs) -> None:
    """record_check + persist, in one call. Used by the orchestrator."""
    _save(record_check(company_key, current_jobs, **kwargs))


# ---- Reads: attach the dates to jobs on their way to a screen -------------

def annotate(jobs: list, company_key: str | None = None,
             data: dict | None = None) -> list:
    """
    Attach first-seen fields to a list of job-ish dicts, returning COPIES (the
    same convention as filters.py's computed flags — never persisted).

    Each job gains:
        first_seen          "YYYY-MM-DD" or None
        first_seen_bounded  True when the date is an upper bound ("on or before")
        first_seen_unclear  True when no date could be established

    company_key - the company these jobs belong to. When None, each job is
                  expected to carry its own `company_key` (saved jobs and
                  applications do; run results don't).

    first_seen_unclear is the honest fallback, exactly like location_unclear:
    a manually-added tracker row never came through a check, and a role whose
    company has since been removed has lost its snapshots. Neither is an error;
    both are "we don't know", and the UI says so rather than guessing.
    """
    data = data if data is not None else load()
    cache = {}
    out = []
    for job in jobs or []:
        j = dict(job)
        key = company_key or j.get("company_key")
        if key not in cache:
            cache[key] = for_company(key, data) if key else {}
        rec = cache[key].get(str(j.get("id")))
        if isinstance(rec, dict) and rec.get("since"):
            j["first_seen"] = rec["since"]
            j["first_seen_bounded"] = bool(rec.get("bounded"))
            j["first_seen_unclear"] = False
        else:
            j["first_seen"] = None
            j["first_seen_bounded"] = False
            j["first_seen_unclear"] = True
        out.append(j)
    return out


# ---- The backfill: rebuild the whole index from the snapshot tree ---------

def rebuild_from_snapshots(company_keys=None, progress=None) -> dict:
    """
    Regenerate the index from scratch by walking every snapshot in date order.

    This is both the one-off backfill for history recorded before the index
    existed AND the repair path if the index ever drifts. It reads only
    snapshots — the source of truth — so it is always safe to re-run, and it
    replaces rather than merges.

    Applies the same rules as record_check: a job's `since` is the start of its
    current unbroken run of sightings, and a job present in a company's
    first-ever snapshot is bounded.

    IMPORTANT — the dormant carry-through can't be reconstructed here. Live, we
    know a clean baseline happened and carry entries through it. Walking the
    tree afterwards, a lapse is indistinguishable from any other gap, so a job
    that spanned a dormant stretch gets its clock restarted at the return. That
    only affects history predating the index and only where a phase lapsed; it
    self-corrects going forward. Documented rather than silently wrong.

    Returns the rebuilt index (and writes it).
    """
    root = paths.data_root() / "snapshots"
    if company_keys is None:
        company_keys = sorted(p.name for p in root.iterdir() if p.is_dir()) \
            if root.exists() else []

    data = _empty()
    for key in company_keys:
        files = storage.list_snapshots(key)      # OLDEST first
        if not files:
            continue
        prev_ids = None
        for i, path in enumerate(files):
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue                          # skip an unreadable snapshot
            jobs = snap.get("jobs") or []
            taken = str(snap.get("taken_at") or "")[:10]
            if not taken:
                continue
            try:
                day = datetime.date.fromisoformat(taken)
            except ValueError:
                continue
            data = record_check(key, jobs,
                                previous_job_ids=prev_ids,
                                today=day,
                                is_first_check=(i == 0),
                                data=data)
            prev_ids = {str(j.get("id")) for j in jobs
                        if j.get("id") not in (None, "")}
        if progress is not None:
            try:
                progress(key, len(for_company(key, data)))
            except Exception:
                pass

    _save(data)
    return data


# Quick manual test:  python3 -m jobwatch.first_seen
if __name__ == "__main__":
    idx = load()
    comps = idx.get("companies", {})
    total = sum(len(v) for v in comps.values())
    print(f"first_seen index: {len(comps)} companies, {total} roles")
    for k in sorted(comps)[:5]:
        rows = comps[k]
        bounded = sum(1 for r in rows.values() if r.get("bounded"))
        print(f"  {k}: {len(rows)} roles ({bounded} bounded)")
