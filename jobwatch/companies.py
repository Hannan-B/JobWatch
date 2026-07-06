"""
companies.py  (Phase C - company store + buckets)
=================================================
This is where JobWatch remembers WHICH companies you're tracking, and which
BUCKETS (groups) each belongs to. It's the list the rest of the app runs over.

Everything here reads and writes one file:
    ~/JobWatchData/companies.json   (a list of company records)

The locked Company shape (see DATA_FORMATS.md):
    {
        "key":          "ogilvy-uk",        # internal unique id, lowercase, no spaces
        "display_name": "Ogilvy UK",
        "connector":    "greenhouse",       # which connector reads it
        "config":       { "board": "ogilvyuk" },
        "buckets":      ["Advertising", "London targets"],
        "tier":         1,                  # 1 auto-detected, 2 preset, 3 not-yet-supported
        "added_on":     "2026-06-15"
    }

A "bucket" isn't a separate file - it's just a label a company carries. The set
of all buckets is simply every label currently in use across your companies.
That keeps buckets dead simple: to put a company in a bucket, add the label;
to run a bucket, gather every company carrying that label.
"""

import json
import datetime
from pathlib import Path

from . import paths
from .connectors import CONNECTORS


def _companies_file() -> Path:
    return paths.data_root() / "companies.json"


def _today_str() -> str:
    return datetime.date.today().isoformat()


class CompanyError(Exception):
    """Raised for company-management problems, with a plain-language message."""


def _load_all() -> list:
    """Read every company record. Returns [] if the file doesn't exist yet."""
    f = _companies_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise CompanyError(
            f"Couldn't read your companies file (it may be corrupted): {f}"
        ) from e
    if not isinstance(data, list):
        raise CompanyError("The companies file isn't in the expected list format.")
    return data


def _save_all(companies: list) -> None:
    paths.ensure_data_dirs()
    _companies_file().write_text(json.dumps(companies, indent=2), encoding="utf-8")


def list_companies() -> list:
    """Return all company records, sorted by display name for stable output."""
    return sorted(_load_all(), key=lambda c: c.get("display_name", "").lower())


def get_company(key: str) -> dict | None:
    """Return one company record by key, or None if not found."""
    for c in _load_all():
        if c.get("key") == key:
            return c
    return None


def add_company(key: str, display_name: str, connector: str, config: dict,
                buckets: list | None = None, tier: int = 1) -> dict:
    """
    Add a new company to the store (or raise if the key already exists).

    key          - internal unique id (lowercase, no spaces). Usually auto-derived
                   from the careers URL by detect.py; can be set explicitly.
    display_name - human-friendly name shown to you.
    connector    - which connector reads it; must be a known connector name.
    config       - the connector's settings (e.g. {"board": "ogilvyuk"}).
    buckets      - optional list of group labels.
    tier         - 1 auto-detected, 2 preset, 3 not-yet-supported.

    A tier-3 company is allowed in the store (so you can remember you want it)
    but won't be runnable until a connector exists - connector may be "" for it.
    """
    key = (key or "").strip().lower()
    display_name = (display_name or "").strip()
    if not key:
        raise CompanyError("A company needs a key (short internal id).")
    if not display_name:
        raise CompanyError("A company needs a display name.")
    if " " in key:
        raise CompanyError("A company key can't contain spaces (use dashes).")

    # Tier 3 can have no connector yet; otherwise it must be a real one.
    if tier != 3:
        if connector not in CONNECTORS:
            raise CompanyError(
                f"Unknown connector '{connector}'. Known: {', '.join(sorted(CONNECTORS))}."
            )

    companies = _load_all()
    if any(c.get("key") == key for c in companies):
        raise CompanyError(
            f"A company with key '{key}' already exists. Keys must be unique."
        )

    record = {
        "key": key,
        "display_name": display_name,
        "connector": connector or "",
        "config": config or {},
        "buckets": sorted(set(buckets or [])),
        "tier": int(tier),
        "added_on": _today_str(),
    }
    companies.append(record)
    _save_all(companies)
    return record


def remove_company(key: str) -> bool:
    """Remove a company by key. Returns True if one was removed."""
    companies = _load_all()
    remaining = [c for c in companies if c.get("key") != key]
    if len(remaining) == len(companies):
        return False
    _save_all(remaining)
    return True


def _update(key: str, mutate) -> dict:
    """Internal: load, find the company, apply mutate(record), save, return it."""
    companies = _load_all()
    for c in companies:
        if c.get("key") == key:
            mutate(c)
            _save_all(companies)
            return c
    raise CompanyError(f"No company with key '{key}'.")


def assign_to_bucket(key: str, bucket: str) -> dict:
    """Add a bucket label to a company (no-op if it already has it)."""
    bucket = (bucket or "").strip()
    if not bucket:
        raise CompanyError("Bucket name can't be empty.")

    def mutate(c):
        labels = set(c.get("buckets", []))
        labels.add(bucket)
        c["buckets"] = sorted(labels)
    return _update(key, mutate)


def remove_from_bucket(key: str, bucket: str) -> dict:
    """Remove a bucket label from a company (no-op if it doesn't have it)."""
    def mutate(c):
        c["buckets"] = sorted(set(c.get("buckets", [])) - {bucket})
    return _update(key, mutate)


def list_buckets() -> list:
    """Return every bucket label currently in use, across all companies."""
    labels = set()
    for c in _load_all():
        labels.update(c.get("buckets", []))
    return sorted(labels)


def companies_in_bucket(bucket: str) -> list:
    """Return all companies carrying a given bucket label."""
    return [c for c in _load_all() if bucket in c.get("buckets", [])]


def rename_bucket(old: str, new: str) -> dict:
    """
    Phase I - Rename a bucket everywhere it's used.

    Because a bucket is just a label every member carries, renaming means
    relabelling every company that has it: remove the old label, add the new one,
    and carry over any sub-bucket placement recorded under the old name. The
    per-bucket sub-bucket NAME roster (sub_buckets.json) is moved too.

    Bucket names are unique: renaming onto an EXISTING different bucket is
    refused (we don't silently merge two buckets). Renaming a bucket to itself
    (or only changing case to the same label) is a harmless no-op.

    Returns a small report: {"old", "new", "companies_updated"}.
    """
    old = (old or "").strip()
    new = (new or "").strip()
    if not old:
        raise CompanyError("Which bucket do you want to rename?")
    if not new:
        raise CompanyError("Give the bucket a new name.")
    if old == new:
        return {"old": old, "new": new, "companies_updated": 0}

    companies = _load_all()
    existing = set()
    for c in companies:
        existing.update(c.get("buckets", []))

    if old not in existing and old not in _load_sub_bucket_names():
        raise CompanyError(f"There's no bucket called '{old}' to rename.")
    if new in existing:
        # The unique-name guard the spec locks: block, never merge.
        raise CompanyError(
            f"There's already a bucket called '{new}'. "
            "Pick a different name, or delete one first."
        )

    updated = 0
    for c in companies:
        labels = c.get("buckets", [])
        if old in labels:
            c["buckets"] = sorted((set(labels) - {old}) | {new})
            updated += 1
        # Carry over a sub-bucket placement recorded under the old bucket name.
        subs = c.get("sub_buckets")
        if isinstance(subs, dict) and old in subs:
            subs[new] = subs.pop(old)
            if not subs:
                c.pop("sub_buckets", None)
    _save_all(companies)

    # Move the sub-bucket name roster for this bucket, if any.
    roster = _load_sub_bucket_names()
    if old in roster:
        roster[new] = roster.pop(old)
        _save_sub_bucket_names(roster)

    return {"old": old, "new": new, "companies_updated": updated}


def delete_bucket(bucket: str) -> dict:
    """
    Phase I - Delete a bucket label from every company that carries it, and drop
    its sub-bucket roster + any per-company sub-bucket placements under it. The
    companies themselves stay (a bucket is only a grouping). Returns how many
    companies were affected, for the reassuring confirm wording.

    (The v1 server did this inline by calling remove_from_bucket per member;
    this gathers the cascade - labels + sub-buckets + roster - in one place so
    nothing about sub-buckets is left dangling.)
    """
    bucket = (bucket or "").strip()
    if not bucket:
        raise CompanyError("Which bucket?")
    companies = _load_all()
    affected = 0
    for c in companies:
        labels = c.get("buckets", [])
        if bucket in labels:
            c["buckets"] = sorted(set(labels) - {bucket})
            affected += 1
        subs = c.get("sub_buckets")
        if isinstance(subs, dict) and bucket in subs:
            subs.pop(bucket, None)
            if not subs:
                c.pop("sub_buckets", None)
    _save_all(companies)

    roster = _load_sub_bucket_names()
    if bucket in roster:
        roster.pop(bucket, None)
        _save_sub_bucket_names(roster)

    return {"bucket": bucket, "companies_kept": affected}


# ---------------------------------------------------------------------------
# Sub-buckets  (Phase I - the one approved engine change in I)
# ===========================================================================
# A sub-bucket is a named subdivision INSIDE one bucket. Unlike buckets (a
# company carries many), a company sits in at most ONE sub-bucket per bucket.
# Anything not explicitly placed is treated as "Other/Misc" - the catch-all
# that makes drag-and-drop safe (a forgotten company is never stranded).
#
# Storage (locked in DATA_FORMATS.md, Phase I):
#   - Per-company PLACEMENT lives on the company record as an optional map:
#         "sub_buckets": { "<bucket name>": "<sub-bucket name>" }
#     Absent bucket key => that company is unplaced => Other/Misc at read time.
#   - Per-bucket NAME ROSTER lives in a small sibling file:
#         ~/JobWatchData/sub_buckets.json  ->  { "<bucket>": ["<sub>", ...] }
#     This exists so a freshly-named-but-empty sub-bucket survives (it has no
#     members yet to carry its name). Read together to render sub-bucket mode.
#
# Both follow the v1 sibling-data-file pattern: external data folder, created
# via paths.ensure_data_dirs(), forgiving reads (a missing/corrupt roster file
# just yields {} - sub-buckets are non-critical, like trends.py).
# ---------------------------------------------------------------------------

OTHER_MISC = "Other/Misc"   # the implicit, always-present catch-all sub-bucket


def _sub_buckets_file() -> Path:
    return paths.data_root() / "sub_buckets.json"


def _load_sub_bucket_names() -> dict:
    """Read the per-bucket sub-bucket NAME roster. Forgiving: missing or
    unreadable file -> {} (sub-buckets are non-critical)."""
    f = _sub_buckets_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce to {str: [str, ...]} defensively.
    out = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if str(x).strip()]
    return out


def _save_sub_bucket_names(roster: dict) -> None:
    paths.ensure_data_dirs()
    _sub_buckets_file().write_text(
        json.dumps(roster, indent=2), encoding="utf-8")


def sub_bucket_names(bucket: str) -> list:
    """
    Return the full, ordered list of sub-bucket names for a bucket, ALWAYS
    including Other/Misc exactly once and last.

    The set is the union of (a) names in the saved roster and (b) names actually
    used by this bucket's members - so a sub-bucket survives whether it has
    members, a roster entry, or both. Other/Misc is appended last regardless.
    """
    bucket = (bucket or "").strip()
    names = []
    seen = set()

    def _add(n):
        n = (n or "").strip()
        if not n or n == OTHER_MISC or n in seen:
            return
        seen.add(n)
        names.append(n)

    for n in _load_sub_bucket_names().get(bucket, []):
        _add(n)
    for c in companies_in_bucket(bucket):
        subs = c.get("sub_buckets")
        if isinstance(subs, dict):
            _add(subs.get(bucket))

    names.sort(key=str.lower)
    names.append(OTHER_MISC)   # always present, always last
    return names


def sub_bucket_of(key: str, bucket: str) -> str:
    """The sub-bucket a company sits in within a bucket, or Other/Misc if it
    isn't explicitly placed. (Does not check that the company is in the bucket;
    callers pass members.)"""
    c = get_company(key)
    if not c:
        return OTHER_MISC
    subs = c.get("sub_buckets")
    if isinstance(subs, dict):
        placed = (subs.get(bucket) or "").strip()
        if placed and placed != OTHER_MISC:
            return placed
    return OTHER_MISC


def set_sub_bucket(key: str, bucket: str, sub: str) -> dict:
    """
    Place a company into a sub-bucket within a bucket (single-occupancy: this
    replaces any previous placement in the SAME bucket). Placing into Other/Misc
    (or clearing) just removes the explicit placement - Other/Misc is the
    implicit home, so we never need to store it.
    """
    bucket = (bucket or "").strip()
    sub = (sub or "").strip()
    if not bucket:
        raise CompanyError("Which bucket is this sub-bucket in?")

    def mutate(c):
        if bucket not in c.get("buckets", []):
            raise CompanyError(
                f"{c.get('display_name', key)} isn't in the '{bucket}' bucket.")
        subs = c.get("sub_buckets")
        if not isinstance(subs, dict):
            subs = {}
        if not sub or sub == OTHER_MISC:
            subs.pop(bucket, None)      # unplaced == Other/Misc
        else:
            subs[bucket] = sub
        if subs:
            c["sub_buckets"] = subs
        else:
            c.pop("sub_buckets", None)
    return _update(key, mutate)


def clear_sub_bucket(key: str, bucket: str) -> dict:
    """Remove a company's explicit sub-bucket placement in a bucket (sends it
    back to Other/Misc). No-op if it had none."""
    return set_sub_bucket(key, bucket, OTHER_MISC)


def sub_bucket_layout(bucket: str) -> dict:
    """
    Everything sub-bucket mode needs for one bucket, in one call:
        {
          "bucket": "Advertising",
          "sub_buckets": ["Creative", "Strategy", "Other/Misc"],  # incl. catch-all
          "companies": [
             {"key","display_name","sub_bucket"},  # sub_bucket resolved (Other/Misc if unplaced)
             ...
          ]
        }
    Only the bucket's own members are listed. Order of companies is by display
    name for a stable board.
    """
    bucket = (bucket or "").strip()
    names = sub_bucket_names(bucket)
    members = sorted(companies_in_bucket(bucket),
                     key=lambda c: c.get("display_name", "").lower())
    out_companies = []
    for c in members:
        out_companies.append({
            "key": c.get("key"),
            "display_name": c.get("display_name"),
            "sub_bucket": sub_bucket_of(c.get("key"), bucket),
        })
    return {"bucket": bucket, "sub_buckets": names, "companies": out_companies}


def save_sub_bucket_layout(bucket: str, names: list, assignments: dict) -> dict:
    """
    Save a whole bucket's sub-bucket layout atomically (the "Save" press in
    sub-bucket mode).

    bucket      - the bucket being organised.
    names       - the user-named sub-buckets for this bucket (Other/Misc is
                  implicit; it's fine if it's present in this list, we ignore it
                  as a stored name since it always exists).
    assignments - {company_key: sub_bucket_name} for this bucket's members.
                  A company mapped to Other/Misc (or omitted) becomes unplaced.
                  An assignment to a name not in `names` is accepted and that
                  name is added to the roster (defensive; the UI shouldn't do
                  this, but we don't lose a placement over it).

    Validation: every key in `assignments` must be a member of `bucket`.
    Returns the fresh layout (so the UI can redraw from the saved truth).
    """
    bucket = (bucket or "").strip()
    if not bucket:
        raise CompanyError("Which bucket are you organising?")

    member_keys = {c.get("key") for c in companies_in_bucket(bucket)}
    assignments = assignments or {}
    for k in assignments:
        if k not in member_keys:
            raise CompanyError(
                f"'{k}' isn't in the '{bucket}' bucket, so it can't be placed "
                "in one of its sub-buckets.")

    # Clean the roster of names (drop blanks + Other/Misc; de-dupe, keep any
    # name an assignment references even if it wasn't in `names`).
    roster_names = []
    seen = set()
    for n in (names or []):
        n = str(n).strip()
        if n and n != OTHER_MISC and n not in seen:
            seen.add(n)
            roster_names.append(n)
    for n in assignments.values():
        n = str(n or "").strip()
        if n and n != OTHER_MISC and n not in seen:
            seen.add(n)
            roster_names.append(n)

    # Persist the roster for this bucket (empty list is allowed = "flat again",
    # which we represent by dropping the bucket from the roster file).
    roster = _load_sub_bucket_names()
    if roster_names:
        roster[bucket] = sorted(roster_names, key=str.lower)
    else:
        roster.pop(bucket, None)
    _save_sub_bucket_names(roster)

    # Apply placements member-by-member (set_sub_bucket handles Other/Misc).
    for key in member_keys:
        set_sub_bucket(key, bucket, assignments.get(key, OTHER_MISC))

    return sub_bucket_layout(bucket)


# Quick manual test:  python3 -m jobwatch.companies
if __name__ == "__main__":
    print("Companies on file:", len(list_companies()))
    for c in list_companies():
        runnable = "runnable" if c["tier"] != 3 else "TIER 3 - not yet runnable"
        print(f"  - {c['display_name']} [{c['key']}] via {c['connector'] or '(none)'} "
              f"({runnable}); buckets: {', '.join(c['buckets']) or '-'}")
    print("Buckets in use:", ", ".join(list_buckets()) or "-")
