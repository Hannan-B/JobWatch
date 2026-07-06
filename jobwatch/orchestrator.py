"""
orchestrator.py  (Phase D.3 + D.4 — run a whole bucket, safely)
===============================================================
This is the piece that ties everything together. Given a bucket (a named group
of companies), it checks each company one at a time and hands back a single
combined result you can read or display.

For each company it runs the chain that already exists in the app:

    fetch_jobs (connectors)  ->  location filter (filters)  ->
    write snapshot (storage) ->  compare to last check (compare)

...all scoped to the CURRENT phase, and with the locked Phase D behaviours:

  * Slow & safe (D.4): one company at a time, never in parallel, with a short
    randomised pause between companies so the checks stay gentle and invisible
    to the sites. (The low-level pacing/headers already live in connectors.py;
    this adds the between-companies spacing and the per-run ordering.)

  * Dormant -> clean baseline (D.2): before comparing, we ask dormancy.py
    whether this phase has lapsed. If it has, this run is a clean baseline:
    everything is reported as new, with no scary "everything removed" churn.

  * Soft-fail (locked): if one company errors, we DO NOT write a snapshot for it
    and DO NOT compare it (so it can never look like "all jobs removed"). It's
    recorded as a failure to troubleshoot, and the run carries on to the rest.

  * Progress events: an optional callback is called as each company starts and
    finishes, so the Phase E interface can drive a progress bar and show the
    "this is deliberately slow so nothing gets flagged" note. With no callback,
    it runs silently.

  * Both report views: the result is shaped so the interface can show EITHER
    a per-company view (each company's new/removed, interest-ranked within) OR
    an all-roles view (every new role across the bucket, ranked together). We
    produce the data once; Phase E renders it two ways.

----------------------------------------------------------------------------
IMPORTANT — cross-module call signatures (verify on first run on the Mac)
----------------------------------------------------------------------------
This module calls storage.py / compare.py / filters.py / companies.py /
connectors.py, whose exact function names live on your Mac. Every such call is
wrapped in a thin adapter below (the _call_* helpers) and marked ASSUMED:. If a
name or argument order differs from the real module, the fix is in ONE place
(the adapter), not scattered through the logic. Run the test script and any
mismatch will name the exact adapter to adjust.
"""

import time
import random
import datetime

from . import phases
from . import dormancy

# The real app modules. These calls are now matched to the actual signatures
# in the user's modules (verified against the real files), not assumed.
from . import connectors
from . import storage
from . import compare
from . import filters
from . import companies
from . import interests as interests_store
from . import trends as trends_store
from . import market_scope  # Phase O — source-side fetch scoping from chosen cities


# Pacing between companies (D.4). We only pause when the NEXT company shares a
# host with the one just checked — that's the single real reason to space
# requests (several boards on one shared platform, e.g. multiple Greenhouse
# boards all served from greenhouse.io). Companies on different hosts run with
# no pause: each site sees only one visit per run, every few days, so spacing
# unrelated sites apart does nothing. The heavy per-request politeness (headers,
# timeouts) already lives in connectors.py.
SAME_HOST_MIN_SECONDS = 1.5
SAME_HOST_MAX_SECONDS = 2.0


class OrchestratorError(Exception):
    """Raised for run-level problems, with a plain-language message."""


# ---------------------------------------------------------------------------
# Thin wrappers around the other modules. Matched to the REAL signatures:
#   companies.companies_in_bucket(bucket)      -> list of company records
#   company record keys: key, display_name, connector, config, buckets, tier
#   connectors.fetch_jobs(provider, config)    -> clean job list
#   filters.apply_all(jobs, interests)         -> {"shown", "matched", ...}
#   storage.write_snapshot(company_key, phase_id, jobs)
#   storage.load_previous_snapshot(company_key, phase_id) -> snapshot | None
#   compare.compare_jobs(previous_jobs, current_jobs)     -> {"new","removed",...}
#   snapshot date key is "taken_at"
# ---------------------------------------------------------------------------

def _company_name(company: dict) -> str:
    """The human-friendly name. Real field is 'display_name'."""
    return company.get("display_name") or company.get("key") or "(unnamed)"


def _is_runnable(company: dict) -> bool:
    """Tier-3 companies have no working connector yet; skip them politely."""
    return company.get("tier") != 3 and bool(company.get("connector"))


def _call_list_bucket_companies(bucket: str) -> list:
    """companies.companies_in_bucket(bucket) -> list of company records."""
    return companies.companies_in_bucket(bucket)


def _call_fetch_jobs(company: dict, the_interests: dict | None = None) -> list:
    """connectors.fetch_jobs(connector, config) -> clean job list.

    Phase O: before fetching, the company's config is SCOPED by the run's chosen
    cities (the_interests["locations_allowed"]). For the big custom boards (Apple,
    Google) this turns "fetch the whole planet, then filter" into "fetch only the
    countries of your chosen cities" — fast and complete (Apple 6411->~118 UK).
    For every other connector the config is returned unchanged. market_scope owns
    the rule (scope only when ALL chosen cities resolve, else fetch everything),
    so this stays a single, safe call. The app's location filter still runs after.
    """
    connector = company.get("connector")
    base_config = company.get("config", {})
    locations = (the_interests or {}).get("locations_allowed") or []
    config = market_scope.scoped_config(connector, base_config, locations)
    return connectors.fetch_jobs(connector, config)


def _call_filter_and_flag(jobs: list, the_interests: dict) -> dict:
    """
    filters.apply_all(jobs, interests) runs the whole locked pipeline:
    location gate -> ranked interest flagging -> experience stretch flag.
    Returns {"shown", "matched", "ambiguous", "elsewhere"}. 'shown' is what we
    track (matched + ambiguous, interest-sorted, experience-flagged): nothing
    possibly-relevant is dropped; only 'elsewhere' is filtered out.
    """
    return filters.apply_all(jobs, the_interests)


def _call_write_snapshot(company_key: str, phase_id: str, jobs: list):
    """storage.write_snapshot(company_key, phase_id, jobs). Note phase_id is
    the SECOND argument and is required."""
    return storage.write_snapshot(company_key, phase_id, jobs)


def _call_load_previous(company_key: str, phase_id: str):
    """storage.load_previous_snapshot(company_key, phase_id) -> snapshot | None.
    Called BEFORE we write today's snapshot, so the latest existing snapshot
    IS the previous check."""
    return storage.load_previous_snapshot(company_key, phase_id)


def _date_from_snapshot(snapshot) -> str | None:
    """Read the date out of an already-loaded snapshot for the dormancy check.
    Real snapshot date key is 'taken_at' (an ISO timestamp); we trim to the
    date part. Returns 'YYYY-MM-DD' or None."""
    if not isinstance(snapshot, dict):
        return None
    val = snapshot.get("taken_at")
    return str(val)[:10] if val else None


def _call_compare(previous_jobs: list, current_jobs: list) -> dict:
    """compare.compare_jobs(previous_jobs, current_jobs) -> dict with
    'new'/'removed'/'unchanged' lists and a 'counts' block. ORDER MATTERS:
    previous first, current second."""
    return compare.compare_jobs(previous_jobs, current_jobs)


def _record_trends(company_key: str, phase_id: str, jobs: list,
                   new_jobs: list, removed_jobs: list,
                   today: datetime.date | None = None,
                   phase_type: str | None = None) -> None:
    """Phase N — record this check's trend counts: open roles (by team/location)
    plus the added/removed deltas, in lockstep. Trends are NON-CRITICAL (the
    locked v1 rule): a failure here must never break a real job check, so we
    swallow any error. Snapshots remain the source of truth; trends are the
    cheap long-term tally layered on top.

    Note this is the FIRST place in the live app that calls record_snapshot_trends
    at all — before Phase N nothing recorded trends, so the chart had no data.
    Recording here, right after the compare, is the single point that has the
    company, the phase, the full current list (for open_count) AND the verdict
    (for added/removed) together."""
    try:
        date = (today or datetime.date.today()).isoformat()
        trends_store.record_snapshot_trends(
            company_key, phase_id, jobs,
            date=date,
            new_jobs=list(new_jobs or []),
            removed_jobs=list(removed_jobs or []),
            phase_type=phase_type,
        )
    except Exception:
        pass  # trends never break a run


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------

def _host_signature(company: dict) -> str:
    """
    A 'which shared host does this company hit' fingerprint, used only to decide
    whether to pause between two companies.

    The key insight: platforms differ in how they serve companies.
      * SHARED-DOMAIN platforms (Greenhouse, Lever, Ashby, SmartRecruiters,
        Workable) serve every company from ONE domain (e.g. all Greenhouse
        boards come from greenhouse.io). The board/company token is just a path
        or parameter — the host is the same. So these group by CONNECTOR alone:
        two Greenhouse boards count as the same host.
      * PER-TENANT platforms (Workday, Pinpoint) give each company its OWN host
        (e.g. <tenant>.myworkdayjobs.com, <subdomain>.pinpointhq.com). Different
        tenants are genuinely different hosts, so these group by connector PLUS
        the tenant/subdomain.
      * CUSTOM connectors (apple, google, eightfold) are each their own host;
        connector name alone is a fine key.
    """
    connector = (company.get("connector") or "").lower()
    config = company.get("config", {}) or {}

    # Per-tenant platforms: the tenant/subdomain identifies a distinct host.
    PER_TENANT = {
        "workday": ("host", "tenant"),   # distinct myworkdayjobs tenants
        "pinpoint": ("subdomain",),       # distinct pinpointhq subdomains
        "eightfold": ("host",),           # distinct eightfold hosts
    }
    if connector in PER_TENANT:
        parts = [str(config.get(f, "")).lower() for f in PER_TENANT[connector]]
        return f"{connector}:{'/'.join(parts)}"

    # Everything else (shared-domain platforms + custom): connector alone is the
    # host. Two Greenhouse boards share greenhouse.io, so they share a host.
    return connector


def _pause_if_same_host(prev_company: dict, next_company: dict,
                        testing: bool) -> bool:
    """
    Pause 1.5-2s ONLY when the next company shares a host with the previous one.
    Different hosts -> no pause (return False). Returns True if it paused, so the
    run can report it. Skipped entirely in testing.
    """
    if prev_company is None:
        return False
    if _host_signature(prev_company) != _host_signature(next_company):
        return False
    if not testing:
        time.sleep(random.uniform(SAME_HOST_MIN_SECONDS, SAME_HOST_MAX_SECONDS))
    return True


def run_bucket(bucket: str,
               progress=None,
               today: datetime.date | None = None,
               testing: bool = False,
               interests: dict | None = None,
               should_cancel=None) -> dict:
    """
    Check every company in `bucket`, one at a time, scoped to the current phase.

    progress  - optional callback(event_dict). Called with {'kind': 'run_start'|
                'company_start'|'company_done'|'run_done', ...} so a UI can drive
                a progress bar. Safe to omit.
    today     - optional fixed 'now' (for testing / a stable run clock).
    testing   - if True, skips the between-company sleeps so tests run fast.
    interests - optional interests record to use; if omitted, loaded from
                interests.json. Passing it explicitly is handy for testing or a
                Phase E "preview with these interests" feature.

    Returns a combined result dict (see _empty_result for its shape). The result
    is structured for BOTH report views; rendering is Phase E.

    (Phase K refactor: the run loop now lives in _run_over_companies so the Jobs
    page can run an explicit SELECTION of companies via run_companies(); this
    function just resolves the bucket to its members and delegates. Behaviour for
    a whole-bucket run is byte-for-byte what it always was.)
    """
    company_list = _call_list_bucket_companies(bucket)
    return _run_over_companies(company_list, label=bucket, progress=progress,
                               today=today, testing=testing, interests=interests,
                               should_cancel=should_cancel)


def run_companies(keys: list,
                  progress=None,
                  today: datetime.date | None = None,
                  testing: bool = False,
                  interests: dict | None = None,
                  label: str | None = None,
                  should_cancel=None) -> dict:
    """
    Phase K — check an explicit SELECTION of companies (by key), one at a time,
    scoped to the current phase. This is what the Jobs page run picker drives:
    you tick individual companies (across a bucket's sub-buckets) and run just
    those. It shares EVERY internal with run_bucket — fetch → filter → snapshot
    → compare, soft-fail, host-aware pacing, progress events, result shaping —
    the only difference is how the company list is chosen.

    keys   - list of company keys to run, in the order given. Unknown keys are
             skipped silently (the picker only offers real ones; this is just
             defensive). Duplicate keys are de-duped, first occurrence wins.
    label  - human label for the result headline (e.g. "5 companies" or the bucket
             name the selection came from). Defaults to a plain count.

    Returns the same combined result dict as run_bucket.
    """
    # Resolve keys → company records, preserving caller order, de-duping.
    seen = set()
    company_list = []
    for k in (keys or []):
        k = (k or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        rec = companies.get_company(k)
        if rec is not None:
            company_list.append(rec)

    if label is None:
        n = len(company_list)
        label = f"{n} {'company' if n == 1 else 'companies'}"

    return _run_over_companies(company_list, label=label, progress=progress,
                               today=today, testing=testing, interests=interests,
                               should_cancel=should_cancel)


def _run_over_companies(company_list: list,
                        label: str,
                        progress=None,
                        today: datetime.date | None = None,
                        testing: bool = False,
                        interests: dict | None = None,
                        should_cancel=None) -> dict:
    """
    The shared run loop behind both run_bucket and run_companies. Given an
    already-resolved list of company records and a label for the result, it runs
    the locked per-company chain over each, with all the Phase D behaviours
    (one-at-a-time, host-aware pacing, soft-fail, dormant clean-baseline) and the
    Phase E progress events. Result shape is unchanged (see _empty_result); the
    'bucket' field carries `label`.

    should_cancel - optional callable returning True when the run should stop.
                    Checked BETWEEN companies (never mid-fetch), so a cancel is
                    cooperative and safe: the company currently being fetched
                    finishes, then the loop stops and finalises what it has so
                    far. The result is marked cancelled (result["cancelled"]=True)
                    and a 'run_cancelled' progress event is emitted. Whatever was
                    checked before the cancel is kept (real snapshots were taken),
                    so a partial run is still a valid, saved result.
    """
    phase = phases.current_phase()
    if phase is None:
        raise OrchestratorError(
            "There's no current phase, so there's nothing to check against. "
            "Start a phase first."
        )
    phase_id = phase["id"]

    # Load your interests ONCE for the whole run (ranked keywords, locations,
    # experience ceiling). An explicit argument overrides the stored file.
    the_interests = interests if interests is not None else interests_store.load_interests()

    result = _empty_result(label, phase)
    result["cancelled"] = False
    total = len(company_list)
    _emit(progress, {"kind": "run_start", "bucket": label,
                     "phase": phase.get("name"), "total": total,
                     "note": "Checking one company at a time. We only pause "
                             "between companies that share a job-board host, so "
                             "requests stay gentle without slowing the whole run."})

    prev_company = None  # last RUNNABLE company checked, for host-aware pacing
    for index, company in enumerate(company_list, start=1):
        # Cooperative cancel checkpoint — between companies only, so we never
        # abandon an in-flight network request. Stop cleanly and keep what we have.
        if should_cancel is not None and should_cancel():
            result["cancelled"] = True
            _emit(progress, {"kind": "run_cancelled", "bucket": label,
                             "checked": len(result["companies"]),
                             "total": total})
            break

        key = company.get("key") or _company_name(company)
        name = _company_name(company)
        _emit(progress, {"kind": "company_start", "index": index,
                         "total": total, "key": key, "name": name})

        # Tier-3 companies have no working connector yet — skip politely, don't
        # treat as a failure. They're in the bucket so you remember you want them.
        if not _is_runnable(company):
            company_result = {
                "key": key, "name": name, "ok": True, "skipped": True,
                "reason": "Not yet supported (no connector built for this site).",
                "new": [], "removed": [], "current": [],
                "unchanged_count": 0, "baseline": False,
            }
            result["companies"].append(company_result)
            result["skipped"].append({"key": key, "name": name})
            _emit(progress, {"kind": "company_done", "index": index,
                             "total": total, "key": key, "name": name,
                             "ok": True, "skipped": True,
                             "new_count": 0, "removed_count": 0})
            continue

        # Pause only if this company shares a host with the previous runnable
        # one (e.g. two Greenhouse boards). Different hosts run with no wait.
        _pause_if_same_host(prev_company, company, testing)

        try:
            # Per-company STAGE progress (Tier 1): emit fetching/filtering/saving
            # for this company so the UI can show a second, per-company bar. Tagged
            # with the company key so the UI knows which company it's about.
            def _stage(stage_name, _key=key, _name=name, _i=index):
                _emit(progress, {"kind": "company_stage", "index": _i,
                                 "total": total, "key": _key, "name": _name,
                                 "stage": stage_name})
            company_result = _check_one_company(company, phase_id,
                                                the_interests, today=today,
                                                stage=_stage,
                                                phase_type=phase.get("type"))
        except Exception as e:  # soft-fail: log, skip, carry on
            company_result = {
                "key": key, "name": name, "ok": False,
                "error": str(e),
                "new": [], "removed": [], "current": [],
                "unchanged_count": 0,
                "baseline": False,
            }
            result["failures"].append({"key": key, "name": name, "error": str(e)})

        result["companies"].append(company_result)
        prev_company = company  # this was a runnable company; remember its host
        _emit(progress, {"kind": "company_done", "index": index, "total": total,
                         "key": key, "name": name, "ok": company_result["ok"],
                         "new_count": len(company_result["new"]),
                         "removed_count": len(company_result["removed"])})

    _finalise(result)
    _emit(progress, {"kind": "run_done", "bucket": label,
                     "checked": result["counts"]["companies_checked"],
                     "failed": result["counts"]["companies_failed"],
                     "total_new": result["counts"]["total_new"],
                     "total_removed": result["counts"]["total_removed"]})
    return result


def _check_one_company(company: dict, phase_id: str, the_interests: dict,
                       today: datetime.date | None = None,
                       stage=None, phase_type: str | None = None) -> dict:
    """Run the full chain for one company. Raises on failure (caller soft-fails).

    stage - optional callable(stage_name) emitting per-company progress so the UI
            can show a second, per-company bar. Called with "fetching" (reaching
            the board), "filtering" (running the filter/flag pipeline), "saving"
            (writing today's snapshot + comparing). No-op when None. Pure progress
            signalling — it never changes what's fetched, filtered, or recorded.
    """
    key = company.get("key") or _company_name(company)
    name = _company_name(company)

    def _stage(s):
        if stage is not None:
            try:
                stage(s)
            except Exception:
                pass  # progress is never allowed to break a run

    # 1. Fetch, then run the locked filter+flag pipeline (location gate ->
    #    ranked interests -> experience flag). 'shown' = matched + ambiguous,
    #    already interest-sorted; nothing possibly-relevant is dropped.
    _stage("fetching")
    raw_jobs = _call_fetch_jobs(company, the_interests)
    _stage("filtering")
    pipeline = _call_filter_and_flag(raw_jobs, the_interests)
    jobs = pipeline["shown"]

    # 2. Capture the most recent EXISTING snapshot ONCE, before we write today's.
    #    This single value drives BOTH decisions below: the dormancy date (when
    #    were we last here?) and what we diff against. Loading it after the write
    #    would be wrong — today's snapshot would become its own "previous".
    previous = _call_load_previous(key, phase_id)
    last_date = _date_from_snapshot(previous)
    clean_baseline = dormancy.should_start_clean_baseline(last_date, today=today)

    # 3. Write today's snapshot (always — this IS the record of today).
    #    NOTE: we snapshot the 'shown' jobs (location-filtered). That keeps the
    #    comparison consistent run-to-run: we compare what you actually track.
    _stage("saving")
    _call_write_snapshot(key, phase_id, jobs)

    # 4. Compare against the captured previous, unless this is a clean baseline.
    if clean_baseline:
        # Phase N — record trends for the baseline: every shown role is "open"
        # and counts as added; nothing removed (the honest first-check picture).
        _record_trends(key, phase_id, jobs, new_jobs=jobs, removed_jobs=[],
                       today=today, phase_type=phase_type)
        return {
            "key": key, "name": name, "ok": True,
            "baseline": True,
            "new": jobs,          # baseline: everything counts as new
            "removed": [],
            "current": jobs,      # K: the full current (filtered) list for this
                                  # company — what the Jobs two-pane view shows
                                  # when you click the company. Same data we just
                                  # fetched & snapshotted; no second fetch.
            "unchanged_count": 0,
            "ambiguous_count": len(pipeline["ambiguous"]),
            "dormant_reset": bool(last_date),  # True if a real lapse triggered it
        }

    previous_jobs = (previous.get("jobs") if isinstance(previous, dict) else None) or []
    verdict = _call_compare(previous_jobs, jobs)   # previous FIRST, current second
    new = list(verdict.get("new", []))
    removed = list(verdict.get("removed", []))
    unchanged = verdict.get("unchanged", [])
    # Phase N — record trends with this check's real added/removed deltas.
    _record_trends(key, phase_id, jobs, new_jobs=new, removed_jobs=removed,
                   today=today, phase_type=phase_type)
    return {
        "key": key, "name": name, "ok": True,
        "baseline": False,
        "new": new,
        "removed": removed,
        "current": jobs,      # K: full current (filtered) list for this company,
                              # interest-sorted exactly like 'new'. Drives the
                              # Jobs two-pane "click a company → all its roles"
                              # view; new roles are highlighted by matching ids.
        "unchanged_count": len(unchanged) if hasattr(unchanged, "__len__") else 0,
        "ambiguous_count": len(pipeline["ambiguous"]),
        "dormant_reset": False,
    }


# ---------------------------------------------------------------------------
# Result shaping (built once, rendered two ways in Phase E)
# ---------------------------------------------------------------------------

def _empty_result(bucket: str, phase: dict) -> dict:
    return {
        "bucket": bucket,   # the run's label: a bucket name, or a selection label
                            # like "5 companies" when run from the Jobs picker (K)
        "phase": {"id": phase.get("id"), "name": phase.get("name"),
                  "type": phase.get("type")},
        "ran_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "companies": [],     # per-company view feeds off this
        "all_new": [],       # all-roles view feeds off this (filled in _finalise)
        "failures": [],
        "skipped": [],       # tier-3 companies in the bucket, not yet runnable
        "counts": {},
    }


def _interest_rank(job: dict) -> int:
    """Sort key for interest ranking: lower rank = better. Missing = last."""
    r = job.get("interest_rank")
    return r if isinstance(r, int) else 10_000


def _finalise(result: dict) -> None:
    """Fill the all-roles view and the headline counts once all companies ran."""
    all_new = []
    total_new = total_removed = checked = failed = skipped = 0
    for c in result["companies"]:
        if c.get("skipped"):
            skipped += 1
            continue  # tier-3 skip: not checked, not failed, no roles
        if c["ok"]:
            checked += 1
        else:
            failed += 1
        total_new += len(c["new"])
        total_removed += len(c["removed"])
        # Tag each new role with its company so the all-roles view can show it.
        for job in c["new"]:
            tagged = dict(job)
            tagged["_company"] = c["name"]
            tagged["_company_key"] = c["key"]
            all_new.append(tagged)

    # All-roles view: every new role across the bucket, ranked together by
    # interest (best first), regardless of company.
    all_new.sort(key=_interest_rank)
    result["all_new"] = all_new

    # Per-company view: rank each company's own new roles by interest too.
    for c in result["companies"]:
        if not c.get("skipped"):
            c["new"].sort(key=_interest_rank)
            # K: keep the full current list interest-sorted as well, so the
            # two-pane "click a company" view shows the most relevant first,
            # exactly like the new list. (It arrives sorted from filters, but we
            # re-sort here so this is the single canonical ordering point.)
            if isinstance(c.get("current"), list):
                c["current"].sort(key=_interest_rank)

    result["counts"] = {
        "companies_checked": checked,
        "companies_failed": failed,
        "companies_skipped": skipped,
        "total_new": total_new,
        "total_removed": total_removed,
    }


def _emit(progress, event: dict) -> None:
    """Call the progress callback if one was given; never let it crash the run."""
    if progress is None:
        return
    try:
        progress(event)
    except Exception:
        pass  # a misbehaving UI callback must not break the actual check
