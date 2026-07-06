"""
server.py  (Phase E.1 — the local web server behind the interface)
==================================================================
This is the engine room of the visible app. It does three things and nothing
more:

  1. Serves the interface (one HTML page + its assets) to your browser.
  2. Exposes a tiny set of local-only JSON endpoints that call straight into the
     real engine modules (phases, companies, interests, settings, orchestrator).
  3. Streams a bucket run's progress to the page live, so you get the "kick it
     off and watch the bar" experience the plan promised.

It uses only Python's standard library (http.server) — nothing to install. The
server listens on 127.0.0.1 (your Mac only); it is never reachable from the
internet. It is meant to be started by app.py, used in your browser, and stopped
when you close the app.

DESIGN NOTE — why a thread + Server-Sent Events for the run:
    orchestrator.run_bucket() is synchronous and paces itself with short sleeps
    between same-host companies. If we ran it inside a normal request the browser
    tab would hang until the whole bucket finished. Instead we run it on a
    background thread and let it push progress events onto a queue; the page opens
    an EventSource ("/api/run/stream") and receives run_start / company_start /
    company_done / run_done as they happen, then the final report. That is exactly
    the data the orchestrator already emits (see §14 of the handover) — we only
    transport it; we add no engine logic here.
"""

import json
import queue
import threading
import datetime
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import paths
from . import phases
from . import companies
from . import interests as interests_store
from . import settings
from . import dormancy
from . import storage
from . import filters
from . import geo
from . import detect as detect_mod
from . import orchestrator
from . import trends as trends_store
from . import saved_jobs as saved_store
from . import applications as applications_store
from .connectors import CONNECTORS


# Where the static interface files live (next to this module, inside the repo).
_WEB_DIR = Path(__file__).resolve().parent / "web"


# ---------------------------------------------------------------------------
# A single in-flight run. The app is single-user and single-window, so at most
# one bucket run happens at a time. We hold its progress queue and final result
# here so the SSE endpoint can stream them.
# ---------------------------------------------------------------------------
class _RunState:
    """One in-flight run. Single-user app, so at most one run at a time.

    Phase M+ (run-reattach/cancel) additions:
    - A retained PROGRESS TALLY (label, total, per-company done list, counts) so
      a status endpoint can answer even after the original stream disconnected.
    - A FAN-OUT of progress events to any number of listeners (each gets its own
      queue), so leaving and re-opening the run screen re-attaches a fresh live
      stream instead of fighting over one drained queue.
    - A cooperative CANCEL flag the orchestrator checks between companies.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.error = None
        self.running = False
        self.cancelled = False
        # Fan-out: each live stream registers a queue here; _emit copies to all.
        self._listeners = []          # list[queue.Queue]
        # Retained snapshot for re-attach / status (survives stream disconnects).
        self.label = None
        self.total = 0
        self.note = ""
        self.done = []                # list of company_done events, in order
        self.started_keys = []        # company_start events seen (for ordering)

    # --- listener fan-out --------------------------------------------------
    def add_listener(self):
        q = queue.Queue()
        with self.lock:
            self._listeners.append(q)
        return q

    def remove_listener(self, q):
        with self.lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _broadcast(self, event):
        with self.lock:
            listeners = list(self._listeners)
        for q in listeners:
            q.put(event)

    # --- lifecycle ---------------------------------------------------------
    def start(self, bucket, testing=False, keys=None, label=None,
              interests=None):
        with self.lock:
            if self.running:
                return False
            self.result = None
            self.error = None
            self.running = True
            self.cancelled = False
            self.label = label or bucket
            self.total = 0
            self.note = ""
            self.done = []
            self.started_keys = []
        t = threading.Thread(target=self._run,
                             args=(bucket, testing, keys, label, interests),
                             daemon=True)
        t.start()
        return True

    def request_cancel(self):
        """Ask the run to stop at the next between-company checkpoint."""
        with self.lock:
            if not self.running:
                return False
            self.cancelled = True
        return True

    def _should_cancel(self):
        with self.lock:
            return self.cancelled

    def status(self):
        """A snapshot the UI can poll on entry to decide what to show."""
        with self.lock:
            return {
                "running": self.running,
                "cancelled": self.cancelled,
                "label": self.label,
                "total": self.total,
                "note": self.note,
                "done": list(self.done),
                "started": list(self.started_keys),
                "has_result": self.result is not None,
                "error": self.error,
            }

    def _run(self, bucket, testing, keys=None, label=None, interests=None):
        def progress(event):
            # Retain the bits the status snapshot needs, then fan out live.
            kind = event.get("kind")
            with self.lock:
                if kind == "run_start":
                    self.total = event.get("total", 0)
                    self.note = event.get("note", "")
                elif kind == "company_start":
                    self.started_keys.append(event.get("key"))
                elif kind == "company_done":
                    self.done.append(event)
            self._broadcast(event)
        try:
            if keys is not None:
                res = orchestrator.run_companies(
                    keys, progress=progress, testing=testing, label=label,
                    interests=interests, should_cancel=self._should_cancel)
            else:
                res = orchestrator.run_bucket(
                    bucket, progress=progress, testing=testing,
                    interests=interests, should_cancel=self._should_cancel)
            with self.lock:
                self.result = res
            # A cancelled run still produced real (partial) snapshots, so we keep
            # and save it like any other — the gone-alerts + last-report apply.
            try:
                _attach_gone_alerts(res)
            except Exception:
                pass
            _save_last_report(res)
        except Exception as e:
            with self.lock:
                self.error = str(e)
            self._broadcast({"kind": "run_error", "message": str(e)})
        finally:
            self._broadcast({"kind": "_done"})  # sentinel: streams may close
            with self.lock:
                self.running = False


_RUN = _RunState()


# ---------------------------------------------------------------------------
# Last-report persistence (Phase G.9).
#
# v1 kept the most recent run's result in memory only (LAST_RESULT on the page),
# so navigating away and back lost it, and an app restart certainly did. G makes
# "the last report" a real, revisitable destination — and the user chose for it
# to survive a restart. We do the LEAST invasive thing: write the final result
# dict the run already produced to one small file in the EXTERNAL data folder,
# and read it back on request.
#
# This is deliberately transport-layer only. It does NOT touch the engine's
# snapshot / compare / trends logic — orchestrator.run_bucket() returns the same
# result it always did; we just also save a copy of it here, the same sibling-
# data-file pattern the v2 plan sanctions for the Phase L tracker. The file
# lives outside the repo (paths.data_root()), so git never sees it (§4).
# ---------------------------------------------------------------------------

def _last_report_file() -> Path:
    return paths.data_root() / "last_report.json"


def _save_last_report(result: dict) -> None:
    """Persist the most recent run's result so it can be revisited later, even
    after the app is restarted. Non-critical: a failure here must never break a
    run, so we swallow write errors (the run already succeeded)."""
    try:
        paths.ensure_data_dirs()
        payload = {
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "result": result,
        }
        _last_report_file().write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_last_report() -> dict | None:
    """Read the saved last report, or None if there isn't one (or it's
    unreadable). Read-only and forgiving, like the trends reader."""
    f = _last_report_file()
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "result" not in data:
        return None
    return data


# ---------------------------------------------------------------------------
# UI preferences (Phase H) — appearance: light | dark | system.
# Persisted to a sibling data file, mirroring last_report above. This is
# transport/persistence only — no engine logic, no settings.py change (that
# store guards engine values; appearance is a pure interface preference). Both
# helpers are forgiving: a missing or unreadable file just yields the default.
# ---------------------------------------------------------------------------

_VALID_COLOR_MODES = ("light", "dark", "system")
_DEFAULT_COLOR_MODE = "system"


def _ui_prefs_file() -> Path:
    return paths.data_root() / "ui_prefs.json"


def _load_color_mode() -> str:
    """Return the saved appearance mode, or the default if unset/unreadable."""
    f = _ui_prefs_file()
    if not f.exists():
        return _DEFAULT_COLOR_MODE
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _DEFAULT_COLOR_MODE
    mode = data.get("color_mode") if isinstance(data, dict) else None
    return mode if mode in _VALID_COLOR_MODES else _DEFAULT_COLOR_MODE


def _save_color_mode(mode: str) -> str:
    """Validate and persist the appearance mode. Returns the stored value.
    Raises ValueError (plain message) if the mode isn't one we recognise."""
    if mode not in _VALID_COLOR_MODES:
        raise ValueError(
            f"Appearance must be one of {', '.join(_VALID_COLOR_MODES)}."
        )
    paths.ensure_data_dirs()
    _ui_prefs_file().write_text(
        json.dumps({"color_mode": mode}, indent=2), encoding="utf-8")
    return mode


# ---------------------------------------------------------------------------
# Phase deletion data-wipe (Post-Phase-O — History cleanup).
#
# Deleting a phase is a TRUE FULL WIPE: as well as removing the phase record
# (phases.delete_phase, called separately), we remove every trace of that
# phase's data. The four data stores tag their rows by phase_id (DATA_FORMATS
# §4/§6/§6b/§6c), so the wipe is: drop snapshot files for that phase, and drop
# rows tagged with that phase from trends.json / applications.json /
# saved_jobs.json.
#
# This is done at the FILE level here (not via per-store helpers) for one
# honest reason: it keeps the change inside the files this work has in hand,
# and the on-disk shapes are fully locked in DATA_FORMATS.md. It only ever
# touches data carrying THE TARGET phase_id, so no other phase can be affected
# (comparison/continuity are always within a phase — the golden rule). Every
# step is forgiving: a missing or unreadable file is simply skipped, never an
# error — a partial wipe should never block removing the phase record.
# ---------------------------------------------------------------------------

def _filter_json_list_by_phase(file_path: Path, list_key, phase_id) -> int:
    """Drop rows whose phase_id == phase_id from a flat-JSON-list data file.

    list_key=None  → the file's top level IS the list (saved_jobs, applications).
    list_key='entries' → the list lives under that key (trends).

    Returns the number of rows removed. Forgiving: a missing/corrupt file is a
    no-op returning 0. Only rewrites the file if something actually changed."""
    if not file_path.exists():
        return 0
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0  # unreadable/corrupt — leave it; a wipe must not crash here

    if list_key is None:
        rows = data if isinstance(data, list) else None
    else:
        rows = data.get(list_key) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return 0

    kept = [r for r in rows
            if not (isinstance(r, dict) and r.get("phase_id") == phase_id)]
    removed = len(rows) - len(kept)
    if removed == 0:
        return 0

    if list_key is None:
        new_data = kept
    else:
        new_data = dict(data)
        new_data[list_key] = kept
    try:
        file_path.write_text(json.dumps(new_data, indent=2), encoding="utf-8")
    except OSError:
        return 0
    return removed


def _wipe_phase_snapshots(phase_id) -> int:
    """Delete every snapshot file tagged with this phase.

    Snapshots live at snapshots/<company-key>/<timestamp>.json, each file
    carrying its own phase_id (DATA_FORMATS §4). We read each file's phase_id
    and remove only the matching ones — snapshots from OTHER phases in the same
    company folder are left untouched. An emptied company folder is then removed
    so no stray empty dirs linger. Returns the count of snapshot files removed.
    Forgiving throughout."""
    snap_root = paths.data_root() / "snapshots"
    if not snap_root.exists():
        return 0
    removed = 0
    try:
        company_dirs = [d for d in snap_root.iterdir() if d.is_dir()]
    except OSError:
        return 0
    for cdir in company_dirs:
        try:
            files = [f for f in cdir.iterdir() if f.suffix == ".json"]
        except OSError:
            continue
        for f in files:
            try:
                snap = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(snap, dict) and snap.get("phase_id") == phase_id:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # If the company folder is now empty, tidy it away.
        try:
            if not any(cdir.iterdir()):
                shutil.rmtree(cdir, ignore_errors=True)
        except OSError:
            pass
    return removed


def _wipe_phase_data(phase_id) -> dict:
    """Remove ALL of a phase's data: snapshots + trends/applications/saved rows.
    Returns a small count report (safe to log / show). Does NOT touch the phase
    record itself — the caller removes that via phases.delete_phase, AFTER this,
    so a phase is never left pointing at half-wiped data."""
    root = paths.data_root()
    return {
        "snapshots": _wipe_phase_snapshots(phase_id),
        "trends": _filter_json_list_by_phase(
            root / "trends.json", "entries", phase_id),
        "applications": _filter_json_list_by_phase(
            root / "applications.json", None, phase_id),
        "saved": _filter_json_list_by_phase(
            root / "saved_jobs.json", None, phase_id),
    }


# ---------------------------------------------------------------------------
# Helpers that assemble the plain-data the interface needs. These call the real
# engine and shape its output for display — no business logic, just gathering.
# ---------------------------------------------------------------------------

def _latest_check_date_across(company_keys, phase_id):
    """The most recent snapshot date (YYYY-MM-DD) across the given companies in
    this phase, or None. Used to drive the cadence nudge and dormancy line."""
    latest = None
    for key in company_keys:
        snap = storage.load_latest_snapshot(key, phase_id)
        if snap and snap.get("taken_at"):
            d = str(snap["taken_at"])[:10]
            if latest is None or d > latest:
                latest = d
    return latest


def _home_state():
    """Everything the home menu and the phase-state banner need in one call."""
    phase = phases.current_phase()
    all_companies = companies.list_companies()
    bucket_names = companies.list_buckets()
    buckets = []
    for b in bucket_names:
        members = companies.companies_in_bucket(b)
        runnable = [c for c in members if c.get("tier") != 3 and c.get("connector")]
        buckets.append({
            "name": b,
            "company_count": len(members),
            "runnable_count": len(runnable),
        })

    state = {
        "phase": None,
        "phase_state": "dormant",   # dormant | active | casual
        "buckets": buckets,
        "company_count": len(all_companies),
        "data_folder": str(paths.data_root()),
        "dormancy_days": settings.dormancy_days(),
    }

    if phase is not None:
        state["phase_state"] = phase.get("type", "active")
        member_keys = [c["key"] for c in all_companies]
        last_date = _latest_check_date_across(member_keys, phase["id"])
        cadence = phases.cadence_state(last_date, phase=phase)
        dorm = dormancy.status(last_date)
        state["phase"] = {
            "id": phase["id"],
            "name": phase["name"],
            "type": phase.get("type", "active"),
            "cadence_days": phase.get("cadence_days"),
            "started_on": phase.get("started_on"),
            "last_check": last_date,
            "cadence": cadence,
            "dormancy": dorm,
        }
    return state


def _connector_catalogue():
    """The known providers and their human-readable descriptions, for the
    add-company screen and the Tier guides."""
    out = []
    for name, (_fn, required, desc) in CONNECTORS.items():
        out.append({"provider": name, "required": required, "description": desc})
    return sorted(out, key=lambda x: x["provider"])


def _settings_state():
    """The adjustable preferences for the settings screen. Includes the bounds
    so the screen can validate before sending (and show them in plain words)."""
    return {
        "dormancy_days": settings.dormancy_days(),
        "dormancy_min": settings.MIN_DORMANCY_DAYS,
        "dormancy_max": settings.MAX_DORMANCY_DAYS,
    }


def _guides_state():
    """The in-app 'how adding companies works' content (E.7). The three-tier
    model is the locked §5 rule; we surface it plainly plus the real list of
    supported boards so the guide never drifts from what the engine can do."""
    cat = _connector_catalogue()
    # Tier 1 = auto-detected from a pasted URL (these are the runnable boards).
    # Tier 2 = built-in presets handled by name (Apple, Google).
    # Tier 3 = not yet supported; honest fallback, recorded as a request.
    preset_names = {"apple", "google"}
    tier1 = [c for c in cat if c["provider"] not in preset_names]
    tier2 = [c for c in cat if c["provider"] in preset_names]
    return {
        "tier1_providers": tier1,
        "tier2_providers": tier2,
    }


def _manage_state():
    """Everything the manage screen needs: every company (with its buckets and
    whether it's runnable) and the set of buckets in use. One call, so the
    screen never has to stitch several requests together."""
    all_companies = companies.list_companies()
    out_companies = []
    for c in all_companies:
        tier = c.get("tier", 1)
        runnable = tier != 3 and bool(c.get("connector"))
        out_companies.append({
            "key": c.get("key"),
            "display_name": c.get("display_name"),
            "connector": c.get("connector") or "",
            "tier": tier,
            "buckets": c.get("buckets", []),
            "sub_buckets": c.get("sub_buckets", {}),
            "runnable": runnable,
            "added_on": c.get("added_on"),
        })

    # Bucket summary: name + how many companies carry it. Deleting a bucket only
    # removes the label, so we report the count plainly for the confirm wording.
    bucket_names = companies.list_buckets()
    buckets = []
    for b in bucket_names:
        members = companies.companies_in_bucket(b)
        buckets.append({"name": b, "company_count": len(members)})

    return {
        "companies": out_companies,
        "buckets": buckets,
    }


def _phase_id_safe(p):
    return p.get("id") if isinstance(p, dict) else None


# ---------------------------------------------------------------------------
# Phase K — Jobs page helpers.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase M — Filters helpers.
#   _coerce_run_filters : turn a request body's optional `filters` object into a
#                         clean interests-shaped dict for a per-run override
#                         (≤5 locations, valid mode), or None when nothing/empty.
#   _reflag_records     : re-run the flagging pipeline over already-STORED records
#                         (saved jobs / applications) so they carry the same tags
#                         the run views show. Re-apply-on-display (M.5), against
#                         CURRENT interests — stored data stays lean and flags
#                         never go stale.
# ---------------------------------------------------------------------------

def _coerce_run_filters(raw):
    """Build a per-run interests override from a request `filters` object, or
    return None (use the saved interests.json) when it's absent or empty.

    Defensive: clamps locations to the 5-max, ignores a bad mode (falls back to
    rank inside the engine anyway), keeps only string list members. This never
    raises — a malformed filter object just yields a sane override or None, so a
    run is never blocked by filter input (the explicit SAVE path is where the
    user gets a hard error for >5 locations)."""
    if not isinstance(raw, dict):
        return None

    def _str_list(v):
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    kw = _str_list(raw.get("keywords_ranked"))
    locs = _str_list(raw.get("locations_allowed"))[:interests_store.MAX_LOCATIONS]
    depts = _str_list(raw.get("departments_allowed"))
    mode = str(raw.get("keywords_mode") or "rank").strip().lower()
    if mode not in ("rank", "filter"):
        mode = "rank"
    dept_mode = str(raw.get("departments_mode") or "filter").strip().lower()
    if dept_mode not in ("rank", "filter"):
        dept_mode = "filter"
    exp = raw.get("experience_years_max")
    try:
        exp = int(exp) if exp not in (None, "") else None
    except (TypeError, ValueError):
        exp = None

    override = {
        "keywords_ranked": kw,
        "keywords_mode": mode,
        "locations_allowed": locs,
        "departments_allowed": depts,
        "departments_mode": dept_mode,
        "experience_years_max": exp,
    }
    # If the object carried no actual filter content, treat as "no override" so
    # the run uses the saved defaults rather than a blank record that would, in
    # filter mode with no keywords, still behave as "no gate" anyway.
    if not (kw or locs or depts or exp is not None) and mode == "rank":
        return None
    return override


def _reflag_records(records, fields=("id", "title", "location", "department", "url")):
    """Re-run the flagging pipeline over stored records so they carry the same
    interest/stretch/location-unclear/department-unclear tags as the run views.

    records - stored saved-job or application dicts. They already hold the
              display fields (id/title/location/department/url); we compute the
              flags from those against the CURRENT interests, then graft the
              flag fields onto COPIES of the originals (keeping every stored
              field — status, notes, saved_on, etc.).

    Re-apply-on-display (M.5): flags reflect today's interests, never stale.

    Crucially, we NEVER drop a stored row here. These roles are already chosen
    (starred) or applied to — they must always show. So we call the flagging
    helpers DIRECTLY (flag_interests in rank mode + flag_experience) rather than
    apply_all, whose location/department gates would exclude rows. The
    location_unclear / department_unclear markers are computed the same way
    apply_all computes them, but only to TAG, never to hide."""
    if not records:
        return list(records or [])
    try:
        the_interests = interests_store.load_interests()
    except Exception:
        the_interests = dict(getattr(interests_store, "EMPTY_INTERESTS", {}))

    probe = [{f: r.get(f) for f in fields} for r in records]

    # Interest flags (rank mode — tag only, never drop). Experience flag.
    flagged = filters.flag_interests(
        probe, the_interests.get("keywords_ranked", []), mode="rank")
    flagged = filters.flag_experience(
        flagged, the_interests.get("experience_years_max"))

    # Unclear markers, computed exactly as apply_all does, but used only to tag.
    locs = the_interests.get("locations_allowed", [])
    depts = the_interests.get("departments_allowed", [])
    loc_split = filters.filter_by_location(probe, locs)
    # Use rank mode here so we get the department_match flag on every row without
    # dropping any (re-flagging never hides a stored/applied role).
    dept_split = filters.filter_by_department(probe, depts, mode="rank")
    loc_unclear_ids = {str(j.get("id")) for j in loc_split["ambiguous"]}
    # Country-only matches pulled in by the city->country fallback carry a flag.
    loc_country_only_ids = {str(j.get("id")) for j in loc_split["matched"]
                            if j.get("location_country_only")}
    # In rank mode everything's in "matched"; no-department rows still get tagged
    # department_unclear via their blank department.
    dept_match_ids = {str(j.get("id")) for j in dept_split["matched"]
                      if j.get("department_match")}
    dept_unclear_ids = {str(j.get("id")) for j in dept_split["matched"]
                        if not (j.get("department") or "").strip()}

    flagged_by_id = {str(j.get("id")): j for j in flagged}

    out = []
    for r in records:
        rec = dict(r)
        rid = str(r.get("id"))
        f = flagged_by_id.get(rid, {})
        rec["interest_rank"] = f.get("interest_rank")
        rec["interest_hits"] = f.get("interest_hits", [])
        rec["experience_required"] = f.get("experience_required")
        rec["experience_stretch"] = bool(f.get("experience_stretch"))
        rec["location_unclear"] = rid in loc_unclear_ids
        rec["location_country_only"] = rid in loc_country_only_ids
        rec["department_unclear"] = rid in dept_unclear_ids
        rec["department_match"] = rid in dept_match_ids
        out.append(rec)
    return out


def _is_dormant_now() -> bool:
    """True if the current hunt has lapsed into dormancy, OR there's no current
    phase at all. This is the same dormancy the reading spine shows, computed
    from the latest check date across all companies in the current phase. Used to
    drive the locked 'saved jobs reset at dormant' rule (saved_jobs.list_saved)."""
    phase = phases.current_phase()
    if phase is None:
        return True
    member_keys = [c["key"] for c in companies.list_companies()]
    last_date = _latest_check_date_across(member_keys, phase["id"])
    try:
        return bool(dormancy.status(last_date).get("dormant"))
    except Exception:
        return False


def _jobs_state():
    """Everything the Jobs landing needs in one call: the current phase context
    (for the reading spine + 'last checked'), the bucket→sub-bucket→company tree
    the run picker offers, and the saved-jobs count for the tab badge."""
    phase = phases.current_phase()
    all_companies = companies.list_companies()
    member_keys = [c["key"] for c in all_companies]
    last_date = None
    last_check_at = None
    if phase is not None:
        last_date = _latest_check_date_across(member_keys, phase["id"])
        # The precise timestamp of the most recent snapshot, for "date and time".
        newest = None
        for key in member_keys:
            snap = storage.load_latest_snapshot(key, phase["id"])
            if snap and snap.get("taken_at"):
                ts = str(snap["taken_at"])
                if newest is None or ts > newest:
                    newest = ts
        last_check_at = newest

    # The pick tree: every bucket, its sub-buckets (incl. Other/Misc), and the
    # runnable companies under each. Tier-3 (unrunnable) companies are listed but
    # flagged so the picker can show-but-not-select them.
    buckets = []
    for b in companies.list_buckets():
        layout = companies.sub_bucket_layout(b)  # {bucket, sub_buckets[], companies[]}
        members_by_key = {c.get("key"): c for c in companies.companies_in_bucket(b)}
        subs = []
        for sub_name in layout["sub_buckets"]:
            members = []
            for entry in layout["companies"]:
                if entry["sub_bucket"] != sub_name:
                    continue
                rec = members_by_key.get(entry["key"], {})
                tier = rec.get("tier", 1)
                runnable = tier != 3 and bool(rec.get("connector"))
                members.append({
                    "key": entry["key"],
                    "display_name": entry["display_name"],
                    "runnable": runnable,
                })
            # Only include a sub-bucket row if it actually has members.
            if members:
                subs.append({"name": sub_name, "companies": members})
        runnable_count = sum(
            1 for c in members_by_key.values()
            if c.get("tier") != 3 and c.get("connector"))
        buckets.append({
            "name": b,
            "company_count": len(members_by_key),
            "runnable_count": runnable_count,
            "sub_buckets": subs,
        })

    state = {
        "phase": None,
        "phase_state": "dormant",
        "last_check": last_date,
        "last_check_at": last_check_at,
        "buckets": buckets,
        "company_count": len(all_companies),
        "saved_count": len(saved_store.list_saved(is_dormant=_is_dormant_now())),
    }
    if phase is not None:
        state["phase_state"] = phase.get("type", "active")
        state["phase"] = {
            "id": phase["id"],
            "name": phase["name"],
            "type": phase.get("type", "active"),
            "cadence_days": phase.get("cadence_days"),
            "started_on": phase.get("started_on"),
            "last_check": last_date,
        }
    return state


def _current_roles_state(company_key: str):
    """Read-only: one company's CURRENT tracked roles — the location-filtered
    'shown' set from its latest snapshot in the current phase, re-flagged against
    today's interests so the same tags (interest rank, stretch, location-unclear)
    show as everywhere else. This is the Phase K 'click a company → all its roles'
    read path: snapshots already exist; only the read is new, no engine change.

    Returns {company:{key,display_name}, roles:[…], taken_at, phase_id} or, when
    there's no snapshot for the company in this phase, an empty roles list."""
    phase = phases.current_phase()
    if phase is None:
        return {"company": None, "roles": [], "taken_at": None, "phase_id": None}
    rec = companies.get_company(company_key)
    display = rec.get("display_name") if rec else company_key
    snap = storage.load_latest_snapshot(company_key, phase["id"])
    if not snap or not isinstance(snap.get("jobs"), list):
        return {"company": {"key": company_key, "display_name": display},
                "roles": [], "taken_at": None, "phase_id": phase["id"]}
    the_interests = interests_store.load_interests()
    # The snapshot already holds the location-filtered 'shown' set (that's what
    # the run snapshots), so re-running apply_all just re-flags/re-sorts; it
    # won't drop anything that location already let through.
    flagged = filters.apply_all(snap["jobs"], the_interests)
    return {
        "company": {"key": company_key, "display_name": display},
        "roles": flagged["shown"],
        "taken_at": snap.get("taken_at"),
        "phase_id": phase["id"],
    }


# ---------------------------------------------------------------------------
# Phase L — Application Tracker helpers.
# The tracker is the DURABLE record of applied roles (survives dormancy, unlike
# saved_jobs). These helpers shape its data for the table and run the locked
# behaviours that belong on the server side: adopting "Applied" saved roles into
# the tracker (then dropping them from saved), and the L.10 run-report alert.
# applications.py owns the auto-ghost + ladder logic; here we only gather/route.
# ---------------------------------------------------------------------------

def _adopt_applied_into_tracker():
    """Move any saved roles the user pressed 'Applied' on into the tracker, then
    remove them from the saved store so an applied role lives ONLY in the tracker
    (L.6). Idempotent and forgiving — safe to call on every tracker read. Returns
    the number of NEW applications created (0 most of the time)."""
    phase = phases.current_phase()
    phase_id = phase["id"] if phase else None
    try:
        return applications_store.adopt_from_saved(saved_store, phase_id)
    except Exception:
        return 0


def _applications_state(phase_id=None):
    """Everything the tracker table needs in one call. Adopts any pending
    'Applied' saved roles first (L.6), then lists this phase's applications with
    the lazy auto-ghost already applied (applications.py handles both). Also
    cross-references each row against the company's latest snapshot in the phase
    so the table can show a 'role no longer listed' marker (L.5).

    phase_id - which phase's applications to show; defaults to the current phase.
               When there's no current phase and none is asked for, returns an
               empty list with phase=None (the dormant/quiet case)."""
    _adopt_applied_into_tracker()

    cur = phases.current_phase()
    target_phase_id = phase_id or (cur["id"] if cur else None)

    phase_rec = None
    if target_phase_id:
        p = phases.get_phase(target_phase_id)
        if p:
            phase_rec = {
                "id": p.get("id"), "name": p.get("name"), "type": p.get("type"),
                "started_on": p.get("started_on"), "ended_on": p.get("ended_on"),
                "is_current": p.get("ended_on") in (None, ""),
            }

    rows = applications_store.list_applications(phase_id=target_phase_id)

    # L.5 — mark rows whose role id is no longer in the company's latest snapshot
    # within this phase. Read-only: reuses the snapshots that already exist. A
    # manual add (synthetic id) won't match a snapshot id, so we only flag rows
    # whose company HAS a snapshot AND whose id is genuinely absent from it.
    snap_ids_by_company = {}
    if target_phase_id:
        for r in rows:
            ck = r.get("company_key")
            if ck and ck not in snap_ids_by_company:
                snap = storage.load_latest_snapshot(ck, target_phase_id)
                if snap and isinstance(snap.get("jobs"), list):
                    snap_ids_by_company[ck] = {str(j.get("id")) for j in snap["jobs"]}
                else:
                    snap_ids_by_company[ck] = None  # no snapshot to compare against

    out_rows = []
    for r in rows:
        rec = dict(r)
        ck = r.get("company_key")
        ids = snap_ids_by_company.get(ck)
        # Only assert "no longer listed" when we have a snapshot to check against
        # AND the id isn't in it. No snapshot -> we can't say, so leave it false.
        rec["no_longer_listed"] = bool(ids is not None and str(r.get("id")) not in ids)
        out_rows.append(rec)

    # M.5 — re-flag on read so tracked rows show interest/stretch/location- and
    # department-unclear tags consistent with the run views. _reflag_records keeps
    # every stored field (status, notes, no_longer_listed, …) and never drops a row.
    out_rows = _reflag_records(out_rows)

    # Distinct company names present, for the table's Company filter dropdown.
    company_names = sorted({r.get("company_name") for r in out_rows if r.get("company_name")})

    return {
        "phase": phase_rec,
        "applications": out_rows,
        "company_names": company_names,
    }


def _attach_gone_alerts(result):
    """Phase L.10 — annotate a finished run RESULT with any tracked applications
    that are still LIVE but whose role has vanished from what the run just saw.

    Read-only against the tracker (applications.py); does NOT change the run's
    new/removed/current data. Adds result['gone_alerts'] = [ {title, company_name,
    company_key, id, url, status}, ... ] — empty when nothing qualifies. The page
    renders these as a calm "heads up" line in the report.

    The run's per-company `current` list is the fresh roster (the live fetch the
    run just did). A live tracked role for a company the run covered, whose id
    isn't in that company's current list, is 'gone'. Companies the run did NOT
    cover are left alone (we didn't look at them this run)."""
    if not isinstance(result, dict):
        return
    phase = phases.current_phase()
    phase_id = phase["id"] if phase else None

    # Build, per company the run covered, the set of role ids still present.
    present_by_company = {}
    for c in result.get("companies", []):
        ck = c.get("key")
        if not ck:
            continue
        cur = c.get("current")
        if isinstance(cur, list):
            present_by_company[ck] = {str(j.get("id")) for j in cur}
        else:
            # A failed/skipped company has no reliable current list — don't use
            # it to declare anything 'gone' (its absence isn't evidence).
            present_by_company[ck] = None

    alerts = []
    try:
        live = applications_store.live_applications(phase_id=phase_id)
    except Exception:
        live = []
    for app in live:
        ck = app.get("company_key")
        if ck not in present_by_company:
            continue  # this company wasn't part of the run
        present = present_by_company[ck]
        if present is None:
            continue  # company errored/skipped — no reliable roster
        if str(app.get("id")) not in present:
            alerts.append({
                "id": app.get("id"),
                "title": app.get("title"),
                "company_key": ck,
                "company_name": app.get("company_name"),
                "url": app.get("url"),
                "status": app.get("status"),
            })
    result["gone_alerts"] = alerts


def _archive_state():
    """Past phases for the dormant 'quiet archive' view. Lists every phase
    oldest-first with its dates, type, and how many companies have snapshots
    recorded in it — enough to browse what happened without loading job detail."""
    out = []
    all_companies = companies.list_companies()
    for p in phases.list_phases():
        pid = p.get("id")
        # Count companies that have at least one snapshot in this phase.
        companies_with_data = 0
        last_date = None
        for c in all_companies:
            snap = storage.load_latest_snapshot(c["key"], pid)
            if snap:
                companies_with_data += 1
                d = str(snap.get("taken_at", ""))[:10]
                if d and (last_date is None or d > last_date):
                    last_date = d
        out.append({
            "id": pid,
            "name": p.get("name"),
            "type": p.get("type"),
            "started_on": p.get("started_on"),
            "ended_on": p.get("ended_on"),
            "is_current": p.get("ended_on") in (None, ""),
            "companies_with_data": companies_with_data,
            "last_check": last_date,
        })
    return {"phases": out}


def _archive_phase_state(phase_id):
    """Phase J — one phase's detail for the History drill-in.

    For the given phase, list the companies that have a snapshot recorded in it,
    each with its last-check date and the number of roles in that company's
    LATEST snapshot WITHIN this phase. Read-only and forgiving (like the trends
    reader): it only reads snapshots that already exist via
    storage.load_latest_snapshot — no new fetching, no engine-logic change. A
    company with no snapshot in this phase is simply omitted.

    Returned shape:
        {
          "phase": {id,name,type,started_on,ended_on,is_current} or None,
          "companies": [ {key, display_name, last_check, role_count}, ... ]
        }
    Companies are ordered by display name for a stable list.
    """
    phase = phases.get_phase(phase_id)
    if phase is None:
        return {"phase": None, "companies": []}

    out_companies = []
    for c in companies.list_companies():     # already display-name sorted
        snap = storage.load_latest_snapshot(c["key"], phase_id)
        if not snap:
            continue
        jobs = snap.get("jobs")
        role_count = len(jobs) if isinstance(jobs, list) else 0
        out_companies.append({
            "key": c.get("key"),
            "display_name": c.get("display_name"),
            "last_check": str(snap.get("taken_at", ""))[:10] or None,
            "role_count": role_count,
        })

    return {
        "phase": {
            "id": phase.get("id"),
            "name": phase.get("name"),
            "type": phase.get("type"),
            "started_on": phase.get("started_on"),
            "ended_on": phase.get("ended_on"),
            "is_current": phase.get("ended_on") in (None, ""),
        },
        "companies": out_companies,
    }


def _trends_state(company_key=None, phase_id=None, axis="department"):
    """Phase F — shape the raw trend rows into chartable series, BANDED BY PHASE.

    Reads trends.entries_for(...) (the counts trends.py has recorded every run
    since Phase B) and reshapes them for a hand-rolled SVG line chart. No new
    fetching, no engine change — this only re-organises rows that already exist.

    axis = "department" (default; the useful one) or "location" — which field
    becomes the set of lines on the chart. Counts are summed across the OTHER
    field, and across companies when no single company is chosen, so each line
    is the total open roles for that team (or place) on each date.

    The golden rule from Phase B holds here: comparison and continuity live
    WITHIN a phase. So every series is split into per-phase BANDS — the chart
    must never draw one continuous line across a six-month gap between phases.
    Each band carries its phase's id/name/type/dates so the UI can label it and
    leave a visible break between bands.

    Returned shape:
        {
          "axis": "department",
          "company_key": <key or None>,
          "phase_id": <id or None>,
          "phases": [ {id,name,type,started_on,ended_on,is_current}, ... ],
          "series": [
             { "label": "Finance",
               "total_latest": 9,            # newest point across all its bands
               "bands": [
                 { "phase_id": "...", "phase_name": "...", "phase_type": "active",
                   "points": [ {"date":"2026-04-02","value":2}, ... ] },
                 ...
               ] },
             ...
          ],
          "empty": <bool>,         # true when there isn't enough to chart
          "date_min": "YYYY-MM-DD" or None,
          "date_max": "YYYY-MM-DD" or None
        }
    """
def _trends_state(company_key=None, phase_id=None, axis="department",
                  company_keys=None, metric="open", locations=None):
    """Phase F — shape the raw trend rows into chartable series, BANDED BY PHASE.

    Reads trends.entries_for(...) (the counts trends.py has recorded every run
    since Phase B) and reshapes them for a hand-rolled SVG line chart. No new
    fetching, no engine change — this only re-organises rows that already exist.

    axis = "department" (default; the useful one) or "location" — which field
    becomes the set of lines on the chart. Counts are summed across the OTHER
    field, and across companies when no single company is chosen, so each line
    is the total open roles for that team (or place) on each date.

    Phase N additions (all backward-compatible — existing callers pass none):
      company_keys - a LIST of company keys to include (the Jobs-style tickbox
                     picker on the Company-hiring sub-tab). When given, it takes
                     precedence over the single company_key. None/empty = all.
      metric       - which number each point's "value" carries: "open" (default,
                     full history), "added", or "removed" (Phase-N deltas; older
                     rows lack them and read 0). Every point ALSO carries open/
                     added/removed so the UI can switch metric with no refetch.
      locations    - a list of city/location filters (mirrors the saved Jobs
                     filter). Matched against the coarse location_bucket the trend
                     rows hold (city-level — the locked "City is all I need"
                     decision). A row whose bucket matches any filter is kept;
                     a country-only bucket goes to an "unclear" pass-through so
                     it's never silently dropped. Empty = no location gate.

    The golden rule from Phase B holds here: comparison and continuity live
    WITHIN a phase. So every series is split into per-phase BANDS — the chart
    must never draw one continuous line across a six-month gap between phases.
    Each band carries its phase's id/name/type/dates so the UI can label it and
    leave a visible break between bands.

    Returned shape (Phase N adds open/added/removed onto each point, and the
    `metric` echo):
        {
          "axis": "department",
          "metric": "open",
          "company_key": <key or None>,
          "company_keys": [<keys>] or None,
          "phase_id": <id or None>,
          "phases": [ {id,name,type,started_on,ended_on,is_current}, ... ],
          "series": [
             { "label": "Finance",
               "total_latest": 9,            # newest point of the chosen metric
               "bands": [
                 { "phase_id": "...", "phase_name": "...", "phase_type": "active",
                   "points": [ {"date":"2026-04-02","value":2,
                                "open":2,"added":2,"removed":0}, ... ] },
                 ...
               ] },
             ...
          ],
          "empty": <bool>,         # true when there isn't enough to chart
          "date_min": "YYYY-MM-DD" or None,
          "date_max": "YYYY-MM-DD" or None
        }
    """
    if axis not in ("department", "location", "company"):
        axis = "department"
    axis_field = {"department": "department",
                  "location": "location_bucket",
                  "company": "company_key"}[axis]
    if metric not in ("open", "added", "removed"):
        metric = "open"

    # Resolve which companies to include. A multi-select list (Phase N) wins over
    # the single key; when neither is given, all companies are summed (v1).
    wanted = None
    if company_keys:
        wanted = {str(k).strip() for k in company_keys if str(k).strip()}
    elif company_key:
        wanted = {str(company_key).strip()}

    # Pull rows. We read ALL (no company filter at the store) when a multi-select
    # is in play, then filter in-memory; for the single-company v1 path we keep
    # the store-level filter for efficiency.
    if wanted is not None and len(wanted) == 1:
        rows = trends_store.entries_for(company_key=next(iter(wanted)), phase_id=phase_id)
    else:
        rows = trends_store.entries_for(company_key=None, phase_id=phase_id)
        if wanted is not None:
            rows = [e for e in rows if str(e.get("company_key")) in wanted]

    # Phase N — optional location filter on the coarse trend bucket (city-level).
    # We match the saved/passed locations against location_bucket directly: a
    # contains-match keeps city buckets, and a country-only bucket is kept as a
    # pass-through (never silently dropped — the locked "ambiguous" principle).
    loc_terms = [str(l).strip().lower() for l in (locations or []) if str(l).strip()]
    if loc_terms:
        COUNTRY_ONLY = {"united kingdom", "uk", "united states", "usa", "us",
                        "remote", "unknown", ""}
        kept = []
        for e in rows:
            bucket = (e.get("location_bucket") or "").strip().lower()
            # City match: the filter term appears in the bucket, or vice-versa
            # (so "London" matches "London" and a filter "Greater London" still
            # catches "London").
            hit = any(t in bucket or bucket in t for t in loc_terms if t)
            if hit or bucket in COUNTRY_ONLY:
                kept.append(e)
        rows = kept

    # Phase lookup so each band can be labelled and ordered by start date.
    phase_recs = {p.get("id"): p for p in phases.list_phases()}
    phase_order = {pid: i for i, p in enumerate(phases.list_phases())
                   for pid in [p.get("id")]}

    # When charting BY COMPANY, turn each row's company_key into its display
    # name for the series label (so the legend reads "Ogilvy UK", not "ogilvy-uk").
    company_name = {}
    if axis == "company":
        for c in companies.list_companies():
            if c.get("key"):
                company_name[c["key"]] = c.get("display_name") or c["key"]

    def phase_label(pid):
        p = phase_recs.get(pid)
        return p.get("name") if p else (pid or "Unknown phase")

    # Aggregate: (series_label, phase_id, date) -> summed {open,added,removed}.
    # Summing because one (date, phase) can hold several rows for a series
    # (e.g. the same department across two locations, or several companies).
    agg = {}
    series_labels = set()
    date_min = date_max = None
    for e in rows:
        raw = (e.get(axis_field) or "").strip()
        if axis == "company":
            label = company_name.get(raw, raw or "Unknown")
        elif axis == "location":
            label = raw or "Unknown"
        else:
            label = raw or "(no team)"
        pid = e.get("phase_id") or "unknown"
        date = (e.get("date") or "")[:10]
        if not date:
            continue
        cell = agg.setdefault((label, pid, date), {"open": 0, "added": 0, "removed": 0, "phase_type": ""})
        cell["open"] += e.get("open_count") or 0
        cell["added"] += e.get("added_count") or 0
        cell["removed"] += e.get("removed_count") or 0
        # Post-Phase-O: carry the per-row phase_type so a phase that switched
        # active<->casual can show the change on the chart. Rows for one (label,
        # phase, date) should share a type; if any row carries one, keep it.
        if not cell["phase_type"] and e.get("phase_type"):
            cell["phase_type"] = e.get("phase_type")
        series_labels.add(label)
        if date_min is None or date < date_min:
            date_min = date
        if date_max is None or date > date_max:
            date_max = date

    # Build per-series, per-phase bands of dated points (sorted by date).
    series = []
    for label in series_labels:
        bands_by_phase = {}
        for (lab, pid, date), cell in agg.items():
            if lab != label:
                continue
            bands_by_phase.setdefault(pid, []).append({
                "date": date,
                "value": cell[metric],          # the chosen metric drives the line
                "open": cell["open"],
                "added": cell["added"],
                "removed": cell["removed"],
                "phase_type": cell.get("phase_type", ""),  # type AT THIS DATE
            })

        bands = []
        for pid, points in bands_by_phase.items():
            points.sort(key=lambda pt: pt["date"])
            p = phase_recs.get(pid, {})
            bands.append({
                "phase_id": pid,
                "phase_name": phase_label(pid),
                "phase_type": p.get("type", ""),
                "started_on": p.get("started_on"),
                "ended_on": p.get("ended_on"),
                "points": points,
            })
        # Order bands by their phase's start date (then id) for a sane time axis.
        bands.sort(key=lambda b: (phase_order.get(b["phase_id"], 1e9), b["phase_id"]))

        # The latest value across all bands = "where this line stands now".
        latest_val = 0
        latest_date = ""
        for b in bands:
            if b["points"]:
                last = b["points"][-1]
                if last["date"] >= latest_date:
                    latest_date, latest_val = last["date"], last["value"]

        series.append({
            "label": label,
            "total_latest": latest_val,
            "bands": bands,
        })

    # Biggest current lines first — that's what the eye wants at the top.
    series.sort(key=lambda s: (-s["total_latest"], s["label"].lower()))

    # A synthesized COMBINED TOTAL series (Phase N rework): sum every cell across
    # all labels, per (phase, date), so the UI can always draw an "overall" line —
    # the default when several companies are selected, and the backdrop the
    # breakdown lines sit against. Built from the same agg, banded the same way,
    # so it obeys the never-across-a-gap rule identically. Not counted toward the
    # empty check (it's derived), and flagged is_total so the UI can style it.
    total_by = {}   # (phase_id, date) -> summed {open,added,removed,phase_type}
    for (lab, pid, date), cell in agg.items():
        acc = total_by.setdefault((pid, date), {"open": 0, "added": 0, "removed": 0, "phase_type": ""})
        acc["open"] += cell["open"]
        acc["added"] += cell["added"]
        acc["removed"] += cell["removed"]
        if not acc["phase_type"] and cell.get("phase_type"):
            acc["phase_type"] = cell["phase_type"]
    total_bands_by_phase = {}
    for (pid, date), acc in total_by.items():
        total_bands_by_phase.setdefault(pid, []).append({
            "date": date, "value": acc[metric],
            "open": acc["open"], "added": acc["added"], "removed": acc["removed"],
            "phase_type": acc.get("phase_type", ""),
        })
    total_bands = []
    for pid, points in total_bands_by_phase.items():
        points.sort(key=lambda pt: pt["date"])
        p = phase_recs.get(pid, {})
        total_bands.append({
            "phase_id": pid, "phase_name": phase_label(pid),
            "phase_type": p.get("type", ""),
            "started_on": p.get("started_on"), "ended_on": p.get("ended_on"),
            "points": points,
        })
    total_bands.sort(key=lambda b: (phase_order.get(b["phase_id"], 1e9), b["phase_id"]))
    total_latest_val = 0
    total_latest_date = ""
    for b in total_bands:
        if b["points"]:
            last = b["points"][-1]
            if last["date"] >= total_latest_date:
                total_latest_date, total_latest_val = last["date"], last["value"]
    total_series = {
        "label": "All selected",
        "total_latest": total_latest_val,
        "bands": total_bands,
        "is_total": True,
    } if total_bands else None

    # "Enough to chart" = at least one series with a band of 2+ points, OR
    # more than one distinct date overall. One lone point is a valid but
    # not-yet-chartable state (Phase F caution: look intentional at n=1).
    distinct_dates = {d for (_l, _p, d) in agg.keys()}
    has_line = any(
        any(len(b["points"]) >= 2 for b in s["bands"]) for s in series
    )
    empty = not series or (len(distinct_dates) < 2 and not has_line)

    # The phase list (for the optional phase filter dropdown), newest first.
    phase_list = []
    for p in reversed(phases.list_phases()):
        phase_list.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "type": p.get("type"),
            "started_on": p.get("started_on"),
            "ended_on": p.get("ended_on"),
            "is_current": p.get("ended_on") in (None, ""),
        })

    return {
        "axis": axis,
        "metric": metric,
        "company_key": company_key,
        "company_keys": sorted(wanted) if wanted is not None else None,
        "phase_id": phase_id,
        "phases": phase_list,
        "series": series,
        "total_series": total_series,
        "empty": empty,
        "date_min": date_min,
        "date_max": date_max,
    }


def _company_picker_state():
    """Phase N — the bucket → sub-bucket → company tree the Company-hiring sub-tab
    uses for its tickbox picker (the same shape the Jobs run picker is built on).

    Read-only over companies.py. Every runnable + tier-3 company appears; we don't
    gate on whether a company HAS trend data (a company with none simply draws an
    empty line, which the chart handles). Shape:
        {
          "buckets": [
            { "name": "Advertising",
              "sub_buckets": [
                 { "name": "Creative",
                   "companies": [ {"key","name"}, ... ] }, ...
                 { "name": "Other/Misc", "companies": [...] }
              ] }, ...
          ],
          "all_companies": [ {"key","name"}, ... ]   # the flat fallback list
        }
    A company in several buckets appears under each (a tree, not a partition) —
    the user ticks wherever they think of it. The flat all_companies list backs
    an "everything" view and the default single-company pick.
    """
    all_recs = companies.list_companies()
    flat = sorted(
        ({"key": c.get("key"), "name": c.get("display_name") or c.get("key")}
         for c in all_recs if c.get("key")),
        key=lambda x: (x["name"] or "").lower())

    buckets_out = []
    for bucket in companies.list_buckets():
        members = companies.companies_in_bucket(bucket)
        # Group members by their sub-bucket within this bucket.
        by_sub = {}
        for c in members:
            sub = companies.sub_bucket_of(c.get("key"), bucket)
            by_sub.setdefault(sub, []).append({
                "key": c.get("key"),
                "name": c.get("display_name") or c.get("key"),
            })
        subs_out = []
        for sub in companies.sub_bucket_names(bucket):
            mem = sorted(by_sub.get(sub, []), key=lambda x: (x["name"] or "").lower())
            # Skip an empty Other/Misc so the tree isn't cluttered, but keep a
            # named sub-bucket even if momentarily empty (it's a real grouping).
            if not mem and sub == companies.OTHER_MISC:
                continue
            subs_out.append({"name": sub, "companies": mem})
        buckets_out.append({"name": bucket, "sub_buckets": subs_out})

    return {"buckets": buckets_out, "all_companies": flat}


# ---- Phase N: tracker trends (the "My applications" sub-tab) ----------------

_TRACKER_LIVE = ("applied", "screening", "interview")
_TRACKER_FUNNEL_ORDER = ("applied", "screening", "interview", "offer")
_TRACKER_TERMINAL = ("ghosted", "offer", "rejected", "withdrawn")


def _tracker_trends_state(phase_id=None, bucket=None, sub_bucket=None,
                          today=None):
    """Phase N — the data behind the "My applications" sub-tab: a funnel of how
    applications flow through the stages, windowed counts, all-time rates, and a
    weekly applications-over-time line. Read-only over applications.py +
    companies.py (for the bucket/sub-bucket filter, resolved LIVE from the
    company's CURRENT bucket — the locked decision).

    phase_id   - which phase to report (defaults to the current phase). The
                 tracker is always phase-scoped (the locked rule).
    bucket     - optional bucket filter; only applications whose company is
                 currently in this bucket are counted.
    sub_bucket - optional sub-bucket within that bucket (needs bucket set).

    Returns:
        {
          "phase": {id,name,type,is_current} or None,
          "buckets": [ "Advertising", ... ],          # for the filter dropdown
          "sub_buckets": [ "Creative", ... ],         # of the chosen bucket
          "bucket": <chosen or None>, "sub_bucket": <chosen or None>,
          "total": <int>,                             # applications in scope
          "funnel": [ {"stage":"applied","label":"Applied","count":N}, ... ],
          "rates": { "screening_rate":0.0-1.0, "interview_rate":..,
                     "offer_rate":.., "ghost_rate":.., "response_rate":.. },
          "windows": { "all": N, "14": N, "7": N },   # applied within window
          "by_status": { "applied":N, "screening":N, ... all 7 ... },
          "weekly": [ {"week_start":"YYYY-MM-DD","count":N}, ... ],  # applied/wk
          "empty": <bool>
        }
    """
    today = today or datetime.date.today()
    cur = phases.current_phase()
    target_phase_id = phase_id or (cur["id"] if cur else None)

    phase_rec = None
    if target_phase_id:
        p = phases.get_phase(target_phase_id)
        if p:
            phase_rec = {
                "id": p.get("id"), "name": p.get("name"), "type": p.get("type"),
                "is_current": p.get("ended_on") in (None, ""),
            }

    rows = applications_store.list_applications(phase_id=target_phase_id, today=today)

    # Bucket / sub-bucket filter — resolved LIVE from the company's current
    # membership (locked: applications follow the company's current bucket).
    bucket = (bucket or "").strip() or None
    sub_bucket = (sub_bucket or "").strip() or None
    if bucket:
        member_keys = {c.get("key") for c in companies.companies_in_bucket(bucket)}
        if sub_bucket:
            member_keys = {k for k in member_keys
                           if companies.sub_bucket_of(k, bucket) == sub_bucket}
        rows = [r for r in rows if r.get("company_key") in member_keys]

    total = len(rows)

    # ---- by-status tally + funnel ----
    by_status = {s: 0 for s in
                 ("applied", "screening", "interview", "ghosted", "offer",
                  "rejected", "withdrawn")}
    for r in rows:
        st = r.get("status", "applied")
        if st in by_status:
            by_status[st] += 1

    # The funnel is CUMULATIVE-REACHED, not current-status: an application now at
    # "interview" also reached "applied" and "screening"; an "offer" reached all.
    # We infer "reached" from current status + the screening_interview flag, since
    # the ladder is one-way (you can't be at interview without having applied).
    reached_applied = total                       # everything was applied
    reached_screening = sum(
        1 for r in rows
        if r.get("status") in ("screening", "interview", "offer")
        or r.get("status") == "rejected" and r.get("screening_interview")
        or r.get("screening_interview"))
    reached_interview = sum(
        1 for r in rows
        if r.get("status") in ("interview", "offer")
        or (r.get("interview_rounds") or 0) > 0)
    reached_offer = by_status["offer"]

    funnel = [
        {"stage": "applied",   "label": "Applied",   "count": reached_applied},
        {"stage": "screening", "label": "Screening", "count": reached_screening},
        {"stage": "interview", "label": "Interview", "count": reached_interview},
        {"stage": "offer",     "label": "Offer",     "count": reached_offer},
    ]

    # ---- all-time rates (within the phase) ----
    def _rate(n):
        return (n / total) if total else 0.0
    rates = {
        "screening_rate": _rate(reached_screening),
        "interview_rate": _rate(reached_interview),
        "offer_rate": _rate(reached_offer),
        "ghost_rate": _rate(by_status["ghosted"]),
        # response = anything beyond a silent applied/ghosted: reached screening+
        # or got any terminal that isn't ghost.
        "response_rate": _rate(reached_screening
                               + by_status["rejected"]
                               + by_status["offer"]
                               + by_status["withdrawn"]
                               - sum(1 for r in rows
                                     if r.get("status") in ("rejected", "offer", "withdrawn")
                                     and (r.get("screening_interview")
                                          or r.get("status") in ("interview", "offer")))),
    }
    # response_rate can double-count; clamp to [0,1] defensively.
    rates["response_rate"] = max(0.0, min(1.0, rates["response_rate"]))

    # ---- windowed APPLIED counts (raw counts, per the locked decision) ----
    def _within(days):
        cutoff = today - datetime.timedelta(days=days)
        n = 0
        for r in rows:
            d = _parse_date(r.get("applied_on"))
            if d is not None and d >= cutoff:
                n += 1
        return n
    windows = {"all": total, "14": _within(14), "7": _within(7)}

    # ---- weekly applications-over-time (Monday-anchored weeks) ----
    weekly_map = {}
    for r in rows:
        d = _parse_date(r.get("applied_on"))
        if d is None:
            continue
        monday = d - datetime.timedelta(days=d.weekday())
        weekly_map[monday] = weekly_map.get(monday, 0) + 1
    weekly = [{"week_start": k.isoformat(), "count": v}
              for k, v in sorted(weekly_map.items())]

    # ---- filter dropdown data ----
    all_buckets = companies.list_buckets()
    sub_list = companies.sub_bucket_names(bucket) if bucket else []

    return {
        "phase": phase_rec,
        "buckets": all_buckets,
        "sub_buckets": sub_list,
        "bucket": bucket,
        "sub_bucket": sub_bucket,
        "total": total,
        "funnel": funnel,
        "rates": rates,
        "windows": windows,
        "by_status": by_status,
        "weekly": weekly,
        "empty": total == 0,
    }


def _parse_date(iso):
    """Parse a 'YYYY-MM-DD' (or longer ISO) string to a date, or None."""
    if not iso:
        return None
    try:
        return datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# The request handler. Routes are deliberately few and obvious.
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    # Quieten the default per-request console logging.
    def log_message(self, *args):
        pass

    # ---- small response helpers ----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404, "Not found")
            return
        data = path.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- GET ----
    def do_GET(self):
        route = self.path.split("?", 1)[0]

        if route == "/" or route == "/index.html":
            self._send_file(_WEB_DIR / "index.html")
            return
        if route in ("/app.js", "/app.css"):
            self._send_file(_WEB_DIR / route.lstrip("/"))
            return

        if route == "/api/home":
            try:
                self._send_json(_home_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/connectors":
            self._send_json({"connectors": _connector_catalogue()})
            return

        if route == "/api/interests":
            try:
                self._send_json({"interests": interests_store.load_interests()})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/geo/check":
            # Phase O — does geo recognise this city? The Jobs/Interests location
            # input calls this when you add a city, to warn (gently) when a city
            # can't be resolved to a fetch region — meaning the big boards (Apple,
            # Google) can't be scoped to it and will fetch everything for that run.
            # Read-only, forgiving: any problem just reports "recognised: true" so
            # the warning never blocks adding a location.
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                city = (qs.get("city", [""])[0] or "").strip()
                region = geo.region_for_city(city) if city else None
                if region:
                    self._send_json({
                        "recognised": True,
                        "country": region.get("country", "").title(),
                    })
                else:
                    self._send_json({"recognised": False, "country": ""})
            except Exception:
                # Never let a geo hiccup block the UI — assume recognised.
                self._send_json({"recognised": True, "country": ""})
            return

        if route == "/api/settings":
            try:
                self._send_json(_settings_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/ui-prefs":
            # Appearance preference (H). Read-only and forgiving; always returns
            # a usable value so the interface can theme on first paint.
            self._send_json({"color_mode": _load_color_mode()})
            return

        if route == "/api/guides":
            try:
                self._send_json(_guides_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/manage":
            try:
                self._send_json(_manage_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/sub-buckets":
            # Phase I — read one bucket's sub-bucket layout (roster incl. the
            # implicit Other/Misc, plus each member's resolved sub-bucket).
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            bucket = (qs.get("bucket", [""])[0] or "").strip()
            if not bucket:
                self._send_json({"error": "Which bucket?"}, status=400)
                return
            try:
                self._send_json({"layout": companies.sub_bucket_layout(bucket)})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/jobs":
            # Phase K — the Jobs landing: phase context, the bucket→sub-bucket→
            # company pick tree, last-check date/time, and the saved count.
            try:
                self._send_json(_jobs_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/current-roles":
            # Phase K — one company's current tracked roles (read-only, from its
            # latest snapshot in the current phase). ?company=<key>.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ckey = (qs.get("company", [""])[0] or "").strip()
            if not ckey:
                self._send_json({"error": "Which company?"}, status=400)
                return
            try:
                self._send_json(_current_roles_state(ckey))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/saved":
            # Phase K — the saved (favourite) roles list. Read-only. The dormancy
            # reset is enforced here: if the hunt has lapsed, the list returns
            # empty and the store is cleared. M.5 — re-flag on read so saved roles
            # show the same interest/stretch/location-/department-unclear tags as
            # the run views (flags reflect today's interests; stored data stays lean).
            try:
                items = saved_store.list_saved(is_dormant=_is_dormant_now())
                items = _reflag_records(items)
                self._send_json({"saved": items})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/applications":
            # Phase L — the application tracker for a phase (default: current).
            # Adopts any pending 'Applied' saved roles, applies the lazy auto-
            # ghost, and cross-references the 'no longer listed' flag. ?phase=<id>.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("phase", [""])[0] or "").strip() or None
            try:
                self._send_json(_applications_state(phase_id=pid))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/archive":
            try:
                self._send_json(_archive_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/archive/phase":
            # Phase J — one phase's per-company role counts for the History
            # drill-in. Read-only; ?id=<phase_id>.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("id", [""])[0] or "").strip()
            if not pid:
                self._send_json({"error": "Which phase?"}, status=400)
                return
            try:
                self._send_json(_archive_phase_state(pid))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/trends":
            # Read-only: shape the recorded trend counts for charting.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            company = (qs.get("company", [""])[0] or "").strip() or None
            phase = (qs.get("phase", [""])[0] or "").strip() or None
            axis = (qs.get("axis", ["department"])[0] or "department").strip()
            # Phase N — multi-company select, metric (open/added/removed), and
            # the optional location filter (comma-separated, mirrors saved Jobs).
            companies_raw = (qs.get("companies", [""])[0] or "").strip()
            company_keys = [k for k in companies_raw.split(",") if k.strip()] or None
            metric = (qs.get("metric", ["open"])[0] or "open").strip()
            locs_raw = (qs.get("locations", [""])[0] or "").strip()
            locations = [l for l in locs_raw.split(",") if l.strip()] or None
            try:
                self._send_json(_trends_state(company_key=company,
                                              phase_id=phase, axis=axis,
                                              company_keys=company_keys,
                                              metric=metric,
                                              locations=locations))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/trends/companies":
            # Phase N — the bucket→sub-bucket→company tree for the picker.
            try:
                self._send_json(_company_picker_state())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/tracker-trends":
            # Phase N — the "My applications" sub-tab: funnel, rates, windowed
            # counts, weekly line. ?phase=&bucket=&sub_bucket=
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("phase", [""])[0] or "").strip() or None
            bucket = (qs.get("bucket", [""])[0] or "").strip() or None
            sub = (qs.get("sub_bucket", [""])[0] or "").strip() or None
            try:
                self._send_json(_tracker_trends_state(phase_id=pid, bucket=bucket,
                                                      sub_bucket=sub))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/last-report":
            # Read-only: the most recent run's saved report (Phase G.9), so it
            # can be revisited without re-running. None when nothing's run yet.
            try:
                saved = _load_last_report()
                if saved is None:
                    self._send_json({"result": None})
                else:
                    self._send_json({"result": saved.get("result"),
                                     "saved_at": saved.get("saved_at")})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/detect":
            # Read-only: turn a pasted URL into a provider/config guess.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            url = (qs.get("url", [""])[0] or "").strip()
            try:
                self._send_json({"detection": detect_mod.detect(url)})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return

        if route == "/api/run/stream":
            self._stream_run()
            return

        if route == "/api/run/status":
            # A cheap snapshot the Jobs screen polls on entry: is a run going,
            # and if so how far along. Lets the UI re-attach instead of being
            # stranded behind "a check is already running".
            self._send_json(_RUN.status())
            return

        self.send_error(404, "Not found")

    # ---- POST ----
    def do_POST(self):
        route = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if route == "/api/run/start":
                # Two ways in: a whole bucket (v1 home picker), or an explicit
                # SELECTION of companies by key (Phase K Jobs picker). If 'keys'
                # is present and non-empty we run the selection; otherwise we fall
                # back to the bucket name. Both drive the same engine run.
                raw_keys = body.get("keys")
                keys = None
                if isinstance(raw_keys, list):
                    keys = [str(k).strip() for k in raw_keys if str(k).strip()]
                label = (body.get("label") or "").strip() or None

                # M — optional per-run filter override from the Jobs filter panel.
                # When absent, the orchestrator uses the saved interests.json
                # (interests=None). When present, these filters apply to THIS run
                # only and do NOT change the saved defaults (that's a separate
                # explicit "Save as my defaults" via /api/interests/save).
                run_interests = _coerce_run_filters(body.get("filters"))

                if keys:
                    started = _RUN.start(None, testing=bool(body.get("testing")),
                                         keys=keys, label=label,
                                         interests=run_interests)
                    if not started:
                        self._send_json({"error": "A check is already running."},
                                        status=409)
                        return
                    self._send_json({"ok": True, "keys": keys})
                    return

                bucket = (body.get("bucket") or "").strip()
                if not bucket:
                    self._send_json(
                        {"error": "Pick at least one company to check."},
                        status=400)
                    return
                started = _RUN.start(bucket, testing=bool(body.get("testing")),
                                     interests=run_interests)
                if not started:
                    self._send_json({"error": "A check is already running."},
                                    status=409)
                    return
                self._send_json({"ok": True, "bucket": bucket})
                return

            if route == "/api/run/cancel":
                # Cooperative cancel: ask the run to stop at the next
                # between-company checkpoint. Returns immediately; the run thread
                # winds down on its own and the stream delivers the partial
                # result. If nothing's running, that's fine — report it.
                asked = _RUN.request_cancel()
                self._send_json({"ok": True, "cancelling": asked})
                return

            # ---- saved (favourite) roles (Phase K) ----
            if route == "/api/saved/add":
                job = body.get("job") or {}
                company_key = (body.get("company_key") or "").strip()
                company_name = (body.get("company_name") or "").strip()
                phase = phases.current_phase()
                phase_id = phase["id"] if phase else None
                try:
                    rec = saved_store.add(job, company_key, company_name, phase_id)
                    self._send_json({"ok": True, "saved": rec})
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                return

            if route == "/api/saved/remove":
                company_key = (body.get("company_key") or "").strip()
                job_id = str(body.get("id") or "").strip()
                removed = saved_store.remove(company_key, job_id)
                self._send_json({"ok": True, "removed": removed})
                return

            if route == "/api/saved/applied":
                company_key = (body.get("company_key") or "").strip()
                job_id = str(body.get("id") or "").strip()
                rec = saved_store.mark_applied(company_key, job_id)
                if rec is None:
                    self._send_json(
                        {"error": "That saved role wasn’t found."}, status=404)
                    return
                # Phase L — adopt it straight into the tracker (and drop it from
                # saved) so it lives only in the tracker from now on (L.6).
                try:
                    _adopt_applied_into_tracker()
                except Exception:
                    pass  # non-fatal; the next tracker read will adopt it anyway
                self._send_json({"ok": True, "saved": rec})
                return

            # ---- application tracker (Phase L) ----
            if route == "/api/applications/add":
                # Manual add (a role seen elsewhere) OR a programmatic add. The
                # server stamps applied_on/status/defaults; the body carries the
                # role fields. company_key is optional for a pure manual add.
                job = body.get("job") or {}
                company_key = (body.get("company_key") or job.get("company_key") or "").strip()
                company_name = (body.get("company_name") or job.get("company_name") or "").strip()
                if not company_key:
                    # Manual adds still need *a* company to group under; use the
                    # name as the key if no key was supplied (lowercased, spaced).
                    if company_name:
                        company_key = company_name.strip().lower().replace(" ", "-")
                    else:
                        self._send_json(
                            {"error": "Which company is this role at?"}, status=400)
                        return
                phase = phases.current_phase()
                phase_id = phase["id"] if phase else None
                try:
                    rec = applications_store.add(job, company_key, company_name, phase_id)
                    self._send_json({"ok": True, "application": rec})
                except applications_store.ApplicationError as e:
                    self._send_json({"error": str(e)}, status=400)
                return

            if route == "/api/applications/update":
                # One row, one change at a time: status / screening / rounds /
                # notes. The store enforces the one-way ladder for status and the
                # forward-signal clock reset rules (L.3/L.5).
                company_key = (body.get("company_key") or "").strip()
                job_id = str(body.get("id") or "").strip()
                pid = (body.get("phase_id") or "").strip() or None
                if not company_key or not job_id:
                    self._send_json({"error": "Which application?"}, status=400)
                    return
                try:
                    if "status" in body:
                        rec = applications_store.update_status(
                            company_key, job_id, body.get("status"), phase_id=pid)
                    elif "screening_interview" in body:
                        rec = applications_store.set_screening_interview(
                            company_key, job_id, bool(body.get("screening_interview")),
                            phase_id=pid)
                    elif "interview_rounds" in body:
                        rec = applications_store.set_rounds(
                            company_key, job_id, body.get("interview_rounds"), phase_id=pid)
                    elif "notes" in body:
                        rec = applications_store.set_notes(
                            company_key, job_id, body.get("notes"), phase_id=pid)
                    else:
                        self._send_json({"error": "Nothing to update."}, status=400)
                        return
                    self._send_json({"ok": True, "application": rec})
                except applications_store.ApplicationError as e:
                    self._send_json({"error": str(e)}, status=400)
                return

            if route == "/api/applications/remove":
                company_key = (body.get("company_key") or "").strip()
                job_id = str(body.get("id") or "").strip()
                pid = (body.get("phase_id") or "").strip() or None
                removed = applications_store.remove(company_key, job_id, phase_id=pid)
                self._send_json({"ok": True, "removed": removed})
                return

            if route == "/api/phase/create":
                name = (body.get("name") or "").strip()
                type_ = (body.get("type") or "active").strip()
                p = phases.create_phase(name, type_=type_)
                self._send_json({"ok": True, "phase": p})
                return

            if route == "/api/phase/end":
                p = phases.end_phase()
                self._send_json({"ok": True, "ended": p})
                return

            if route == "/api/phase/switch":
                # Switch the current phase's type in place (warm switch — keeps
                # comparison history). Post-Phase-O: an optional `name` lets the
                # continuation flow relabel the phase in the same step, so the
                # user's "same phase, new name" choice is one atomic action.
                type_ = (body.get("type") or "").strip()
                p = phases.switch_type(type_)
                new_name = (body.get("name") or "").strip()
                if new_name and new_name != p.get("name"):
                    p = phases.rename_phase(new_name, phase_id=p.get("id"))
                self._send_json({"ok": True, "phase": p})
                return

            if route == "/api/phase/rename":
                # Post-Phase-O — rename a phase without touching anything else.
                new_name = (body.get("name") or "").strip()
                pid = (body.get("id") or "").strip() or None
                if not new_name:
                    self._send_json({"error": "Give the phase a name."}, status=400)
                    return
                p = phases.rename_phase(new_name, phase_id=pid)
                self._send_json({"ok": True, "phase": p})
                return

            if route == "/api/phase/delete":
                # Post-Phase-O — History cleanup. DELETE a phase outright: a true
                # full wipe of its data (snapshots + trends/applications/saved),
                # then the phase record itself. The current phase may be deleted
                # (the app then drops to dormant — see phases.delete_phase).
                # Order matters: wipe data FIRST, remove the record SECOND, so a
                # failure can never leave a live phase pointing at gone data.
                phase_id = (body.get("id") or "").strip()
                if not phase_id:
                    self._send_json({"error": "Which phase should I delete?"},
                                    status=400)
                    return
                # Confirm it exists before wiping anything.
                target = phases.get_phase(phase_id)
                if target is None:
                    self._send_json(
                        {"error": "That phase doesn’t exist (already deleted?)."},
                        status=404)
                    return
                was_current = target.get("ended_on") in (None, "")
                try:
                    wiped = _wipe_phase_data(phase_id)
                    removed = phases.delete_phase(phase_id)
                except phases.PhaseError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({
                    "ok": True,
                    "deleted": removed,
                    "wiped": wiped,
                    "was_current": was_current,
                })
                return

            # ---- company management (E.5) ----
            if route == "/api/company/add":
                key = (body.get("key") or "").strip().lower()
                display_name = (body.get("display_name") or "").strip()
                connector = (body.get("connector") or "").strip()
                config = body.get("config") or {}
                buckets = body.get("buckets") or []
                tier = int(body.get("tier") or 1)
                rec = companies.add_company(
                    key=key, display_name=display_name, connector=connector,
                    config=config, buckets=buckets, tier=tier)
                self._send_json({"ok": True, "company": rec})
                return

            if route == "/api/company/remove":
                key = (body.get("key") or "").strip()
                removed = companies.remove_company(key)
                if not removed:
                    self._send_json({"error": f"No company found with key '{key}'."},
                                    status=404)
                    return
                self._send_json({"ok": True, "removed": key})
                return

            if route == "/api/company/buckets":
                # Set a company's bucket membership to an exact list (the manage
                # screen sends the full desired set; we diff against current).
                key = (body.get("key") or "").strip()
                desired = sorted({str(b).strip() for b in (body.get("buckets") or []) if str(b).strip()})
                current_rec = companies.get_company(key)
                if current_rec is None:
                    self._send_json({"error": f"No company found with key '{key}'."},
                                    status=404)
                    return
                current = set(current_rec.get("buckets", []))
                for b in desired:
                    if b not in current:
                        companies.assign_to_bucket(key, b)
                for b in current:
                    if b not in desired:
                        companies.remove_from_bucket(key, b)
                self._send_json({"ok": True, "key": key, "buckets": desired})
                return

            if route == "/api/bucket/assign":
                key = (body.get("key") or "").strip()
                bucket = (body.get("bucket") or "").strip()
                rec = companies.assign_to_bucket(key, bucket)
                self._send_json({"ok": True, "company": rec})
                return

            if route == "/api/bucket/delete":
                # Deleting a bucket removes the LABEL from every company that
                # carries it. The companies themselves stay — a bucket is only a
                # grouping. We report how many were affected so the UI can show
                # the reassuring count it already promised the user. (Phase I:
                # this now cascades through companies.delete_bucket so a bucket's
                # sub-bucket placements + name roster are cleaned up too.)
                bucket = (body.get("bucket") or "").strip()
                if not bucket:
                    self._send_json({"error": "Which bucket?"}, status=400)
                    return
                rep = companies.delete_bucket(bucket)
                self._send_json({"ok": True, "bucket": rep["bucket"],
                                 "companies_kept": rep["companies_kept"]})
                return

            if route == "/api/bucket/rename":
                # Phase I — rename a bucket everywhere (label on every member +
                # any sub-bucket placements + the name roster). Unique names:
                # renaming onto an existing different bucket is refused by the
                # engine with a plain message, surfaced as a 400 below.
                old = (body.get("old") or "").strip()
                new = (body.get("new") or "").strip()
                rep = companies.rename_bucket(old, new)
                self._send_json({"ok": True, **rep})
                return

            # ---- sub-buckets (Phase I) ----
            if route == "/api/sub-buckets/save":
                # Save a whole bucket's sub-bucket layout atomically (the "Save"
                # press in sub-bucket mode). Body: {bucket, names[], assignments}.
                bucket = (body.get("bucket") or "").strip()
                names = body.get("names") or []
                assignments = body.get("assignments") or {}
                if not isinstance(names, list):
                    names = []
                if not isinstance(assignments, dict):
                    assignments = {}
                layout = companies.save_sub_bucket_layout(bucket, names, assignments)
                self._send_json({"ok": True, "layout": layout})
                return

            # ---- interests (E.6) ----
            if route == "/api/interests/save":
                # The screen always sends the COMPLETE desired record (lists may
                # be empty; experience may be null to mean "no ceiling"). The
                # engine's save_interests treats experience=None as "keep current"
                # (it's a partial-update helper), so it cannot by itself CLEAR a
                # previously-set ceiling. We therefore save the always-expressible
                # fields through the engine, then, when the user has cleared the
                # ceiling, persist that explicitly via the engine's own loader and
                # validated saver — so the screen can truly blank it. (No engine
                # change: we reuse interests.load_interests / save path.)
                kw = body.get("keywords_ranked")
                locs = body.get("locations_allowed")
                kw = [str(k) for k in kw] if isinstance(kw, list) else []
                locs = [str(l) for l in locs] if isinstance(locs, list) else []

                # M — the keyword mode (rank/filter) and department filter. Both
                # optional: a caller that doesn't send them leaves them untouched
                # (save_interests skips None args). The Interests screen sends
                # lists/strings; the Jobs "Save as my defaults" path does too.
                depts = body.get("departments_allowed")
                depts = [str(d) for d in depts] if isinstance(depts, list) else None
                mode = body.get("keywords_mode")
                mode = str(mode) if isinstance(mode, str) and mode.strip() else None
                dept_mode = body.get("departments_mode")
                dept_mode = str(dept_mode) if isinstance(dept_mode, str) and dept_mode.strip() else None

                raw_exp = body.get("experience_years_max", None)
                clearing = raw_exp in (None, "")

                try:
                    if clearing:
                        # Save the always-expressible fields via the engine
                        # (incl. M's mode/departments), then clear experience.
                        interests_store.save_interests(
                            keywords_ranked=kw, locations_allowed=locs,
                            keywords_mode=mode, departments_allowed=depts,
                            departments_mode=dept_mode)
                        rec = interests_store.load_interests()
                        rec["experience_years_max"] = None
                        interests_store.paths.ensure_data_dirs()
                        interests_store._interests_file().write_text(
                            json.dumps(rec, indent=2), encoding="utf-8")
                    else:
                        rec = interests_store.save_interests(
                            keywords_ranked=kw, locations_allowed=locs,
                            experience_years_max=raw_exp,
                            keywords_mode=mode, departments_allowed=depts,
                            departments_mode=dept_mode)
                except interests_store.InterestsError as e:
                    # e.g. >5 locations, or a bad keyword mode — plain message.
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"ok": True, "interests": interests_store.load_interests()})
                return

            # ---- settings (E.7 — dormancy threshold) ----
            if route == "/api/settings/save":
                if "dormancy_days" in body:
                    settings.set_dormancy_days(body.get("dormancy_days"))
                self._send_json({"ok": True, "settings": _settings_state()})
                return

            if route == "/api/ui-prefs":
                # Persist the appearance choice (H). Validation lives in the
                # helper; a bad value surfaces as a plain 400 like the rest.
                saved = _save_color_mode((body.get("color_mode") or "").strip())
                self._send_json({"ok": True, "color_mode": saved})
                return

            self.send_error(404, "Not found")
        except Exception as e:
            # Engine raises plain-language messages; surface them as-is.
            self._send_json({"error": str(e)}, status=400)

    # ---- the live run stream (Server-Sent Events) ----
    def _stream_run(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Snapshot first so a RE-ATTACHING client (opened the run screen while a
        # run was already going) immediately sees the correct label/total and the
        # company rows already completed — then it streams live from here on. The
        # part that scrolled by while they were away can't be perfectly replayed,
        # but counts + bar + finished rows are all correct.
        snap = _RUN.status()
        if not snap["running"] and not snap["has_result"]:
            self._sse({"kind": "idle"})
            return

        # Register as a live listener BEFORE replaying, so no event is missed in
        # the gap between snapshot and subscribe (duplicates are harmless — the
        # client keys company rows by company key).
        q = _RUN.add_listener()
        try:
            # Replay the retained snapshot as synthetic events.
            self._sse({"kind": "run_start", "bucket": snap["label"],
                       "total": snap["total"], "note": snap["note"],
                       "reattach": True})
            for ev in snap["done"]:
                self._sse(ev)
            # If the run already finished (result ready) deliver it and close.
            if not snap["running"]:
                with _RUN.lock:
                    if _RUN.error:
                        self._sse({"kind": "run_failed", "message": _RUN.error})
                    elif _RUN.result is not None:
                        self._sse({"kind": "result", "result": _RUN.result})
                self._sse({"kind": "end"})
                return

            # Live tail.
            while True:
                try:
                    event = q.get(timeout=30)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except OSError:
                        break
                    continue

                if event.get("kind") == "_done":
                    with _RUN.lock:
                        if _RUN.error:
                            self._sse({"kind": "run_failed", "message": _RUN.error})
                        elif _RUN.result is not None:
                            self._sse({"kind": "result", "result": _RUN.result})
                    self._sse({"kind": "end"})
                    break

                self._sse(event)
        finally:
            _RUN.remove_listener(q)

    def _sse(self, obj):
        try:
            self.wfile.write(b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n")
            self.wfile.flush()
        except OSError:
            pass


def make_server(host="127.0.0.1", port=8765):
    """Create (but don't start) the threading HTTP server."""
    return ThreadingHTTPServer((host, port), _Handler)
