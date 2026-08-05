"""
applications.py  (Phase L — the Application Tracker store)
=========================================================
This is JobWatch's durable record of the roles you've APPLIED to. It is the
companion to saved_jobs.py, but with the opposite lifetime: saved jobs are
convenience data that reset when a hunt goes dormant; applications are the real
record of your job search and SURVIVE dormancy. They are tied to the phase they
were created in, and read back as a table you manage.

Everything here reads and writes one file:
    ~/JobWatchData/applications.json

It follows the same sibling-data-file pattern as saved_jobs.py / trends.py
(external data folder, created via paths.ensure_data_dirs(), forgiving reads — a
missing or corrupt file yields an empty list). It deliberately does NOT touch
the engine's snapshot / compare / trends logic. The one cross-store link is the
"adopt from saved" migration (adopt_from_saved), which moves roles you pressed
"Applied" on in the Jobs tab out of saved_jobs and into here.

The locked Application shape (DATA_FORMATS.md §6c, Session 21):
    {
        "id":                  "4673971005",          # role id (opaque string)
        "title":               "Social Strategy Director",
        "company_key":         "ogilvy-uk",
        "company_name":        "Ogilvy UK",
        "url":                 "https://.../jobs/4673971005",
        "phase_id":            "phase-2026-04-active", # the phase it was created in
        "applied_on":          "2026-06-21",          # date row created (AUTO)
        "status":              "applied",             # one of the 8 (see below)
        "screening_interview": false,                 # the Yes/No column
        "interview_rounds":    0,                     # editable count
        "notes":               "",                    # the user's OWN thinking
        "last_progress_at":    "2026-06-21"           # date of last FORWARD signal
    }

THE 8 STATUSES (grouped):
    To-do      : applied                              (default on add)
    In progress: screening, interview
    Complete   : ghosted, offer, rejected_before_interview,
                 rejected_after_interview, withdrawn
                 (+ LEGACY bare "rejected", still valid, never offered)

    LIVE    = applied / screening / interview   (can still progress, can auto-ghost)
    TERMINAL= everything else                   (lifecycle ended)

THE ONE-WAY LADDER (locked): applied -> screening -> interview, forward only.
No backward or sideways live moves. The terminal statuses can be set directly
from any live status. update_status() enforces this; an illegal move raises
ApplicationError with a plain message.

THE AUTO-GHOST RULE (locked): a row that is still LIVE and whose last forward
signal was >= the auto-ghost threshold ago auto-flips to "ghosted".
last_progress_at starts at applied_on and is bumped ONLY by a forward signal — a
forward status move OR the screening-interview toggle going No->Yes. Editing
notes / rounds never bumps it. Evaluated LAZILY ON READ (the app has no
background timer): every read runs apply_auto_ghost and persists any flips, so
the change is durable. A row "becomes" ghosted on the next read after it crosses
the threshold, not at the precise midnight — identical in practice, but the flip
is dated to the read.

THE THRESHOLD IS A SETTING, NOT A CONSTANT (2026-08-05). It moved from a
hardcoded 14 to settings.ghost_after_days() (default 21), adjustable from the
Settings screen. Two consequences worth knowing before you touch this:

  * THE FLIP IS DESTRUCTIVE. apply_auto_ghost overwrites `status` and keeps no
    record of what was there, so a row that was at "screening" when it ghosted
    is afterwards indistinguishable from one that was at "applied". RAISING the
    threshold therefore does NOT un-ghost rows already flipped at the old value
    — that needs a deliberate one-off correction, and the old status has to be
    reconstructed by hand or inferred from screening_interview / interview_rounds.
    (This is exactly what the 14 -> 21 change cost: a handful of rows had to be
    corrected by hand, and one of them could not be reconstructed from the data
    at all.)
  * A CORRECTION CANNOT GO THROUGH update_status. "ghosted" is TERMINAL, and
    _is_legal_transition refuses terminal -> live by design. Any un-ghosting is
    a direct write to the file, not an API call. Do not weaken the ladder to
    make a backfill convenient.

IDENTITY / DEDUPE: an application is identified by (company_key, id). Adding a
role already tracked in the SAME PHASE is a no-op (returns the existing row).
Manual adds may lack an engine id; we synthesise a stable local id then.
"""

import json
import datetime
from pathlib import Path

from . import paths
from . import settings


# ---- The status model -----------------------------------------------------

STATUS_APPLIED = "applied"
STATUS_SCREENING = "screening"
STATUS_INTERVIEW = "interview"
STATUS_GHOSTED = "ghosted"
STATUS_OFFER = "offer"
STATUS_WITHDRAWN = "withdrawn"

# Rejections carry their STAGE (added 2026-07-25). Knowing whether a rejection
# came before anyone spoke to you, or after you'd interviewed, is the difference
# between "the market isn't biting" and "I'm getting in the room but not closing"
# — and it's what makes a response rate meaningful (see server._employer_engaged).
STATUS_REJECTED_BEFORE_INTERVIEW = "rejected_before_interview"
STATUS_REJECTED_AFTER_INTERVIEW = "rejected_after_interview"

# LEGACY. Rows written before the split carry a bare "rejected". They stay valid
# and readable for ever — we do NOT migrate them, exactly as pre-Phase-N trend
# rows keep working without delta fields. Readers treat a bare "rejected" as
# "stage unknown" and fall back to the engagement markers (screening_interview /
# interview_rounds). The UI no longer OFFERS it, so it can only shrink over time.
STATUS_REJECTED = "rejected"

REJECTION_STATUSES = (STATUS_REJECTED_BEFORE_INTERVIEW,
                      STATUS_REJECTED_AFTER_INTERVIEW,
                      STATUS_REJECTED)

# The live ladder, in forward order. Index position defines "forward".
LIVE_LADDER = (STATUS_APPLIED, STATUS_SCREENING, STATUS_INTERVIEW)
TERMINAL_STATUSES = (STATUS_GHOSTED, STATUS_OFFER,
                     STATUS_REJECTED_BEFORE_INTERVIEW,
                     STATUS_REJECTED_AFTER_INTERVIEW,
                     STATUS_REJECTED,
                     STATUS_WITHDRAWN)
ALL_STATUSES = LIVE_LADDER + TERMINAL_STATUSES

# Statuses the UI should OFFER. Legacy "rejected" is accepted on the way in (so
# old rows can still be corrected) but never presented as a new choice.
SELECTABLE_STATUSES = tuple(s for s in ALL_STATUSES if s != STATUS_REJECTED)


def is_rejection(status: str) -> bool:
    """True for any rejection, legacy or staged."""
    return (status or "") in REJECTION_STATUSES

# Days of silence (since the last forward signal) before a live row auto-ghosts.
# This USED to be a module constant of 14. It is now a setting so it can be
# changed from the Settings screen without a code change — settings.py owns the
# default (21), so there is exactly one source of truth for the number.
def ghost_after_days() -> int:
    """The current auto-ghost threshold in days.

    Falls back to the built-in default if the settings file is unreadable: a
    corrupt settings file must never stop the tracker from listing, in keeping
    with this store's forgiving-read rule. NOT the dormancy threshold — they
    share a default of 21 and nothing else (see settings.ghost_after_days)."""
    try:
        return settings.ghost_after_days()
    except Exception:
        return int(settings.DEFAULTS["ghost_after_days"])


class ApplicationError(Exception):
    """Raised for tracker problems, with a plain-language message safe to show."""


def _applications_file() -> Path:
    return paths.data_root() / "applications.json"


def _today() -> datetime.date:
    return datetime.date.today()


def _today_str() -> str:
    return _today().isoformat()


def _now_local_id() -> str:
    """A stable local id for a manually-added role that has no engine id."""
    return "manual-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")


# ---- Raw load / save (forgiving, like saved_jobs.py) ----------------------

def _load_raw() -> list:
    """Read the applications list. Forgiving: missing/unreadable/corrupt -> []."""
    f = _applications_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        # Keep only dict entries that at least have an id + company_key.
        if isinstance(r, dict) and r.get("id") and r.get("company_key"):
            out.append(r)
    return out


def _save_raw(records: list) -> None:
    paths.ensure_data_dirs()
    _applications_file().write_text(json.dumps(records, indent=2), encoding="utf-8")


def _same(record: dict, company_key: str, job_id: str) -> bool:
    return (record.get("company_key") == company_key
            and str(record.get("id")) == str(job_id))


# ---- The auto-ghost rule (lazy, on every read) ----------------------------

def _days_between(earlier_iso: str, today: datetime.date) -> int | None:
    """Whole days from an ISO date string to `today`. None if unparseable."""
    if not earlier_iso:
        return None
    try:
        earlier = datetime.date.fromisoformat(str(earlier_iso)[:10])
    except (ValueError, TypeError):
        return None
    return (today - earlier).days


def apply_auto_ghost(records: list, today: datetime.date | None = None,
                     threshold_days: int | None = None) -> tuple:
    """
    Flip any LIVE row whose last forward signal was >= the auto-ghost threshold
    ago to 'ghosted'. Pure-ish: returns (new_records, changed_count). Does NOT
    write — callers persist if changed_count > 0.

    A row qualifies when: status is live AND today - last_progress_at >= the
    threshold. last_progress_at falls back to applied_on (then to None -> never
    ghosts) if missing, so older records written before this field still behave
    sanely.

    threshold_days - optional override; defaults to settings.ghost_after_days().
                     Injectable in the same way (and for the same reason) as
                     dormancy.is_dormant's threshold_days: tests must be able to
                     pin a number without writing to the real settings file.

    NOTE: this OVERWRITES status and keeps no record of the previous value. See
    the module docstring — raising the threshold does not undo past flips.
    """
    today = today or _today()
    if threshold_days is None:
        threshold_days = ghost_after_days()
    changed = 0
    out = []
    for r in records:
        rec = dict(r)
        if rec.get("status") in LIVE_LADDER:
            anchor = rec.get("last_progress_at") or rec.get("applied_on")
            gap = _days_between(anchor, today)
            if gap is not None and gap >= threshold_days:
                rec["status"] = STATUS_GHOSTED
                changed += 1
        out.append(rec)
    return out, changed


def _read_with_ghost(today: datetime.date | None = None,
                     threshold_days: int | None = None) -> list:
    """Load, apply the lazy auto-ghost, persist if anything flipped, return."""
    records = _load_raw()
    records, changed = apply_auto_ghost(records, today=today,
                                        threshold_days=threshold_days)
    if changed:
        _save_raw(records)
    return records


# ---- Reads ----------------------------------------------------------------

def list_applications(phase_id: str | None = None,
                      today: datetime.date | None = None) -> list:
    """
    Return applications, newest-applied first, after applying the lazy auto-ghost.

    phase_id - when given, only this phase's applications are returned (the
               tracker's default view is the current phase). When None, returns
               every phase's applications (used by History drill-ins).
    """
    records = _read_with_ghost(today=today)
    if phase_id is not None:
        records = [r for r in records if r.get("phase_id") == phase_id]
    records.sort(key=lambda r: (r.get("applied_on", ""), r.get("last_progress_at", "")),
                 reverse=True)
    return records


def get_application(company_key: str, job_id: str,
                    phase_id: str | None = None) -> dict | None:
    """Return one application by (company_key, id), optionally scoped to a phase."""
    for r in _read_with_ghost():
        if _same(r, company_key, job_id):
            if phase_id is None or r.get("phase_id") == phase_id:
                return r
    return None


def is_tracked(company_key: str, job_id: str, phase_id: str | None = None) -> bool:
    """True if this role is already in the tracker (optionally in this phase)."""
    return get_application(company_key, job_id, phase_id=phase_id) is not None


# ---- Add (manual + the Applied adoption share this) -----------------------

def add(job: dict,
        company_key: str,
        company_name: str,
        phase_id: str | None,
        applied_on: str | None = None) -> dict:
    """
    Create an application row (status 'applied'). Idempotent within a phase: if
    the same (company_key, id) is already tracked in this phase, the existing row
    is returned unchanged (never duplicated — the locked dedupe).

    job - {id, title, location, department, url, ...} as it appears in a run
          result / saved record. For a MANUAL add with no engine id, we match on
          url if we can, else synthesise a local id so the row has a stable key.
    """
    company_key = (company_key or "").strip()
    if not company_key:
        raise ApplicationError("An application needs a company.")

    job = job or {}
    job_id = str(job.get("id") or "").strip()
    url = (job.get("url") or "").strip()

    records = _load_raw()

    # Dedupe by (company_key, id) within the phase.
    if job_id:
        for r in records:
            if _same(r, company_key, job_id) and r.get("phase_id") == phase_id:
                return r
    elif url:
        # Manual add without an id: fall back to URL match within the phase.
        for r in records:
            if (r.get("company_key") == company_key
                    and (r.get("url") or "").strip() == url
                    and r.get("phase_id") == phase_id):
                return r

    if not job_id:
        job_id = _now_local_id()

    applied = (applied_on or _today_str())[:10]
    record = {
        "id": job_id,
        "title": job.get("title") or "(untitled role)",
        "company_key": company_key,
        "company_name": (company_name or company_key).strip(),
        "url": url,
        "phase_id": phase_id,
        "applied_on": applied,
        "status": STATUS_APPLIED,
        "screening_interview": False,
        "interview_rounds": 0,
        "notes": "",
        "last_progress_at": applied,   # starts equal to applied_on
    }
    records.append(record)
    _save_raw(records)
    return record


def remove(company_key: str, job_id: str, phase_id: str | None = None) -> bool:
    """Delete an application. Returns True if one was removed."""
    records = _load_raw()
    def keep(r):
        if not _same(r, company_key, job_id):
            return True
        if phase_id is not None and r.get("phase_id") != phase_id:
            return True
        return False
    remaining = [r for r in records if keep(r)]
    if len(remaining) == len(records):
        return False
    _save_raw(remaining)
    return True


# ---- Update: status ladder, screening toggle, rounds, notes ---------------

def _ladder_index(status: str) -> int | None:
    try:
        return LIVE_LADDER.index(status)
    except ValueError:
        return None


def _is_legal_transition(current: str, new: str) -> bool:
    """
    Legal moves:
      * to any TERMINAL status, from any LIVE status (and re-setting a terminal
        status — e.g. correcting offer->rejected — is allowed).
      * forward one-or-more steps along the live ladder (applied->screening,
        applied->interview, screening->interview).
      * no-op (current == new) is allowed (handled by the caller as a no-op).
    Illegal:
      * any backward/sideways live move (interview->screening, screening->applied).
      * leaving a terminal status back into a live one (a closed application is
        closed; re-open by removing + re-adding if truly needed).
    """
    if current == new:
        return True
    if new in TERMINAL_STATUSES:
        # From a live status, or correcting one terminal to another.
        return True
    if new in LIVE_LADDER:
        ci = _ladder_index(current)
        ni = _ladder_index(new)
        if ci is None:
            # current is terminal -> cannot go back to a live status.
            return False
        return ni > ci   # forward only
    return False


def update_status(company_key: str, job_id: str, new_status: str,
                  today: datetime.date | None = None,
                  phase_id: str | None = None) -> dict:
    """
    Move a row's status, enforcing the one-way ladder + terminal rules. A FORWARD
    live move bumps last_progress_at to today (the auto-ghost clock resets).
    Setting a terminal status does NOT bump it (the clock is irrelevant once the
    lifecycle ends). Returns the updated row.
    """
    new_status = (new_status or "").strip()
    if new_status not in ALL_STATUSES:
        raise ApplicationError(f"'{new_status}' isn't a valid status.")

    records = _load_raw()
    target = None
    for r in records:
        if _same(r, company_key, job_id) and (phase_id is None or r.get("phase_id") == phase_id):
            target = r
            break
    if target is None:
        raise ApplicationError("That application wasn’t found.")

    current = target.get("status", STATUS_APPLIED)
    if not _is_legal_transition(current, new_status):
        raise ApplicationError(
            f"You can’t move an application from “{current}” to “{new_status}”. "
            "Applications move forward (applied → screening → interview) or to a "
            "final outcome; they don’t go backwards."
        )

    if current == new_status:
        return target  # no-op

    # A forward LIVE move is a forward signal -> reset the clock.
    moved_forward_live = (new_status in LIVE_LADDER
                          and _ladder_index(new_status) is not None
                          and _ladder_index(current) is not None
                          and _ladder_index(new_status) > _ladder_index(current))

    target["status"] = new_status
    if moved_forward_live:
        target["last_progress_at"] = (today or _today()).isoformat()
    _save_raw(records)
    return target


def set_screening_interview(company_key: str, job_id: str, value: bool,
                            today: datetime.date | None = None,
                            phase_id: str | None = None) -> dict:
    """
    Set the screening-interview Yes/No flag. A No->Yes flip is a FORWARD signal
    and resets the auto-ghost clock (locked). Yes->No (a correction) does not.
    """
    records = _load_raw()
    target = None
    for r in records:
        if _same(r, company_key, job_id) and (phase_id is None or r.get("phase_id") == phase_id):
            target = r
            break
    if target is None:
        raise ApplicationError("That application wasn’t found.")

    new_val = bool(value)
    was = bool(target.get("screening_interview"))
    target["screening_interview"] = new_val
    if (not was) and new_val:
        # No -> Yes is a forward signal: reset the clock (once).
        target["last_progress_at"] = (today or _today()).isoformat()
    _save_raw(records)
    return target


def set_rounds(company_key: str, job_id: str, rounds: int,
               phase_id: str | None = None) -> dict:
    """Set interview_rounds. NEVER bumps last_progress_at (not a forward signal)."""
    try:
        rounds = max(0, int(rounds))
    except (ValueError, TypeError):
        raise ApplicationError("Rounds of interview must be a whole number.")
    records = _load_raw()
    target = None
    for r in records:
        if _same(r, company_key, job_id) and (phase_id is None or r.get("phase_id") == phase_id):
            target = r
            break
    if target is None:
        raise ApplicationError("That application wasn’t found.")
    target["interview_rounds"] = rounds
    _save_raw(records)
    return target


def _sanitize_notes_html(html: str) -> str:
    """Reduce notes HTML to a safe restricted subset on the server side (never
    trust the client). Keeps only b/strong/i/em/u/ul/ol/li/p/br tags and drops
    ALL attributes; unknown tags are unwrapped (their text is kept). This guards
    the stored data even if a request bypasses the in-browser editor.

    Implemented with a tiny tag-aware filter over the raw string (no external
    deps, matching the project's stdlib-only rule). It is intentionally strict:
    anything it doesn't recognise as an allowed tag becomes inert text or is
    dropped, so no href/onclick/style/script can survive."""
    if not isinstance(html, str) or not html:
        return ""
    import re as _re
    allowed = {"b", "strong", "i", "em", "u", "ul", "ol", "li", "p", "br"}

    # First, remove dangerous elements INCLUDING their content (so the inner
    # text of <script>/<style> never leaks through as plain text). Handles an
    # unclosed trailing tag too.
    html = _re.sub(r"(?is)<(script|style)\b.*?(</\1\s*>|$)", "", html)

    def _tag_sub(m):
        raw = m.group(0)
        closing = raw[1] == "/"
        # tag name = letters right after '<' or '</'
        name_m = _re.match(r"</?\s*([a-zA-Z0-9]+)", raw)
        name = (name_m.group(1).lower() if name_m else "")
        # Browsers wrap Enter-separated lines in <div>; map div -> p so the line
        # break SURVIVES (matches the client sanitizer). Without this, the server
        # re-strips divs on save and notes collapse onto one line.
        if name == "div":
            name = "p"
        if name in allowed:
            # rebuild WITHOUT any attributes (drop everything but the tag name)
            return f"</{name}>" if closing else f"<{name}>"
        return ""  # disallowed tag -> remove the tag (keep any text outside it)

    # Replace every tag token; text between tags is left untouched.
    cleaned = _re.sub(r"<[^>]*>", _tag_sub, html)
    return cleaned.strip()


def set_notes(company_key: str, job_id: str, notes: str,
              phase_id: str | None = None) -> dict:
    """Set the rich-text notes (a restricted HTML subset; sanitized here on the
    server regardless of the client). NEVER bumps last_progress_at (not a forward
    signal — notes are the user's own thinking, not employer response)."""
    clean = _sanitize_notes_html(notes if isinstance(notes, str) else str(notes or ""))
    records = _load_raw()
    target = None
    for r in records:
        if _same(r, company_key, job_id) and (phase_id is None or r.get("phase_id") == phase_id):
            target = r
            break
    if target is None:
        raise ApplicationError("That application wasn’t found.")
    target["notes"] = clean
    _save_raw(records)
    return target


# ---- The "Applied" hand-off: adopt from saved_jobs ------------------------

def adopt_from_saved(saved_module, phase_id: str | None,
                     today: datetime.date | None = None) -> int:
    """
    Move roles the user pressed "Applied" on in the Jobs tab out of saved_jobs
    and into the tracker. Returns how many NEW applications were created.

    saved_module - the saved_jobs module (passed in so this stays decoupled and
                   testable). We read saved_module.list_applied(), create an
                   application for each not-already-tracked role, then call
                   saved_module.remove() so the role lives ONLY in the tracker.

    Idempotent: a role already tracked (in this phase) is skipped via add()'s
    dedupe but STILL removed from saved (so a second adoption is a clean no-op).
    """
    created = 0
    try:
        applied_saved = saved_module.list_applied()
    except Exception:
        return 0

    for s in applied_saved:
        company_key = (s.get("company_key") or "").strip()
        job_id = str(s.get("id") or "").strip()
        if not company_key or not job_id:
            continue
        # The phase the role was saved in is the truest home; fall back to the
        # phase passed in (current) if the saved record didn't carry one.
        target_phase = s.get("phase_id") or phase_id
        existed = is_tracked(company_key, job_id, phase_id=target_phase)
        job = {
            "id": job_id,
            "title": s.get("title"),
            "url": s.get("url"),
            "location": s.get("location"),
            "department": s.get("department"),
        }
        # applied_on: prefer when they marked it applied, else today.
        applied_on = (s.get("applied_on") or today and today.isoformat()
                      or _today_str())[:10]
        add(job, company_key, s.get("company_name") or company_key,
            target_phase, applied_on=applied_on)
        if not existed:
            created += 1
        # Either way, remove from saved so it lives only here.
        try:
            saved_module.remove(company_key, job_id)
        except Exception:
            pass
    return created


# ---- L.10 support: which tracked roles are still "live" -------------------

def live_applications(phase_id: str | None = None,
                      today: datetime.date | None = None) -> list:
    """
    The applications still in a LIVE status (applied/screening/interview) after
    the lazy auto-ghost. This is what the run-report alert (L.10) checks against
    the fresh snapshot: a live tracked role absent from what the run just saw is
    flagged. Terminal-status rows are intentionally excluded (their disappearance
    is expected and silent).
    """
    return [r for r in list_applications(phase_id=phase_id, today=today)
            if r.get("status") in LIVE_LADDER]


# Quick manual test:  python3 -m jobwatch.applications
if __name__ == "__main__":
    demo = {"id": "1", "title": "Test Role", "url": "http://example.com/1"}
    print("Starting count:", len(list_applications()))
    add(demo, "demo-co", "Demo Co", "phase-demo")
    add(demo, "demo-co", "Demo Co", "phase-demo")  # idempotent
    print("After add (twice attempted):", len(list_applications()))
    update_status("demo-co", "1", "screening")
    print("Status now:", get_application("demo-co", "1")["status"])
    try:
        update_status("demo-co", "1", "applied")  # illegal backward
    except ApplicationError as e:
        print("Backward blocked:", str(e)[:40], "...")
    remove("demo-co", "1")
    print("After remove:", len(list_applications()))
