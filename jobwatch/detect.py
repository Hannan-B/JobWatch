"""
detect.py  (Phase C.1 - auto-detect provider from a pasted URL)
===============================================================
The magic that makes adding a company painless: you paste a careers URL, and
this works out WHICH connector reads it and WHAT settings that connector needs -
no codes, no tokens, no jargon.

How it works: most job boards put their identity right in the URL. Greenhouse
URLs contain "greenhouse.io/<token>", Lever URLs contain "lever.co/<token>",
Workday URLs look like "<tenant>.wd3.myworkdayjobs.com/.../<site>", and so on.
We match the URL against known patterns and pull out the bits the connector needs.

What this returns (a "detection result"):
    {
        "provider":     "greenhouse" | ... | None,   # None = couldn't identify
        "config":       { ... },        # settings for that connector
        "suggested_key":"ogilvyuk",     # a derived internal key, or "" if unsure
        "display_guess":"Ogilvy",       # a friendly name guess, or ""
        "tier":         1 | 2 | 3,
        "confident":    True | False,   # False = we guessed; please confirm
        "message":      "plain-language explanation for the user",
    }

HONEST FALLBACK (the §5 three-tier rule): if we can't identify a supported board,
we don't pretend. We return provider=None, tier=3, and a clear message that this
site needs a connector built before it can be tracked. The URL is still worth
recording as a "request a connector" to-do.

Some providers are RECOGNISED here but not yet BUILT (Workday, Pinpoint, etc.).
For those we can still detect the provider and extract config, but until the
connector exists they behave as "recognised, connector pending" - flagged so the
add flow can tell you plainly.
"""

import re
import urllib.parse

from .connectors import CONNECTORS


# Providers we can RECOGNISE from a URL. Whether each is actually RUNNABLE
# depends on whether its connector exists in CONNECTORS (checked at the end).
# Each entry: a function(url, host, path, query) -> config dict or None.

def _slug_from_path(path: str) -> str:
    """First non-empty path segment, e.g. '/ogilvyuk/jobs' -> 'ogilvyuk'."""
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else ""


def _detect_greenhouse(url, host, path, query):
    # boards.greenhouse.io/<token>  OR  job-boards.greenhouse.io/<token>
    # OR boards-api.greenhouse.io/v1/boards/<token>/...
    if "greenhouse.io" not in host:
        return None
    m = re.search(r"/v1/boards/([^/]+)", path)
    if m:
        return {"board": m.group(1)}
    token = _slug_from_path(path)
    return {"board": token} if token else None


def _detect_lever(url, host, path, query):
    # jobs.lever.co/<token>   OR  api.lever.co/v0/postings/<token>
    if "lever.co" not in host:
        return None
    m = re.search(r"/v0/postings/([^/?]+)", path)
    if m:
        return {"board": m.group(1)}
    token = _slug_from_path(path)
    return {"board": token} if token else None


def _detect_ashby(url, host, path, query):
    # jobs.ashbyhq.com/<token>
    if "ashbyhq.com" not in host:
        return None
    token = _slug_from_path(path)
    return {"board": token} if token else None


def _detect_smartrecruiters(url, host, path, query):
    # jobs.smartrecruiters.com/<company>  OR  careers.smartrecruiters.com/<company>
    if "smartrecruiters.com" not in host:
        return None
    company = _slug_from_path(path)
    return {"company": company} if company else None


def _detect_workable(url, host, path, query):
    # apply.workable.com/<account>/  OR  <account>.workable.com
    if "workable.com" not in host:
        return None
    if host.endswith(".workable.com") and not host.startswith("apply."):
        account = host.split(".")[0]
        return {"account": account} if account and account != "www" else None
    account = _slug_from_path(path)
    return {"account": account} if account else None


def _detect_eightfold(url, host, path, query):
    # Eightfold tenants vary; the careers HOST is the key, and Eightfold also
    # needs a "domain" — the company's real WEB domain, which it filters on.
    # We recognise "*.eightfold.ai" and the common "explore.jobs.<company>.<tld>"
    # careers hosts.
    #
    # The domain CANNOT be read reliably from the host: the careers host's TLD is
    # often NOT the company's web TLD. Netflix's careers host is
    # "explore.jobs.netflix.net", but Eightfold's domain filter must be
    # "netflix.com" — deriving it by stripping the prefix (-> "netflix.net")
    # yields a 404. Since Eightfold "domain" values are overwhelmingly .com, we
    # take the registrable ROOT name and default the TLD to .com. A rare company
    # whose real domain isn't .com can be corrected after adding, but .com is the
    # correct guess for the common case (and the confirmed Netflix case).
    if "eightfold.ai" in host or host.startswith("explore.jobs."):
        stripped = host.replace("explore.jobs.", "").replace("careers.", "")
        # stripped is like "netflix.net" / "netflix.com" / "acme.co.uk". Take the
        # root label (the part before the first dot) and default the TLD to .com.
        root = stripped.split(".")[0] if stripped else stripped
        domain = f"{root}.com" if root else stripped
        return {"host": host, "domain": domain}
    return None


def _detect_workday(url, host, path, query):
    # <tenant>.wd{N}.myworkdayjobs.com/<locale?>/<site>
    # also jobs.myworkdaysite.com/recruiting/<tenant>/<site>
    m = re.match(r"^([^.]+)\.wd\d+\.myworkdayjobs\.com$", host)
    if m:
        tenant = m.group(1)
        # path may be "/en-GB/Wellcome" or "/Wellcome"; the site is the last
        # non-locale segment.
        parts = [p for p in path.split("/") if p]
        parts = [p for p in parts if not re.match(r"^[a-z]{2}-[A-Z]{2}$", p)]
        site = parts[0] if parts else tenant
        # Reconstruct the data-center host (wd1/wd3/...) exactly as given.
        return {"host": host, "tenant": tenant, "site": site}
    m = re.match(r"^jobs\.myworkdaysite\.com$", host)
    if m:
        parts = [p for p in path.split("/") if p]
        # /recruiting/<tenant>/<site>
        if len(parts) >= 3 and parts[0] == "recruiting":
            return {"host": host, "tenant": parts[1], "site": parts[2]}
    return None


def _detect_pinpoint(url, host, path, query):
    # <company>.pinpointhq.com/...  (e.g. harpercollins.pinpointhq.com)
    if not host.endswith(".pinpointhq.com"):
        return None
    sub = host.split(".")[0]
    return {"subdomain": sub} if sub and sub != "www" else None


def _detect_webitrent(url, host, path, query):
    # *.webitrent.com - MHR iTrent web recruitment (UK public-sector / cultural).
    # The connector needs the full launch URL (it carries the tenant + WVID);
    # we also surface the WVID so a short key can be derived.
    if "webitrent.com" not in host:
        return None
    cfg = {"url": url}
    m = re.search(r"[?&]WVID=([^&]+)", url)
    if m:
        cfg["wvid"] = m.group(1)
    cfg["host"] = host
    return cfg


def _detect_ciphr(url, host, path, query):
    # *.ciphr-irecruit.com - CIPHR iRecruit (UK public-sector / cultural).
    # The connector needs the vacancy-list URL (it carries the tenant subdomain).
    if "ciphr-irecruit.com" not in host:
        return None
    return {"url": url, "host": host}


# Order matters only for readability; hosts are mutually exclusive in practice.
# tier: the natural tier when detected. confident: whether the extraction is safe.
_RECOGNISERS = [
    ("greenhouse",      _detect_greenhouse,      1),
    ("lever",           _detect_lever,           1),
    ("ashby",           _detect_ashby,           1),
    ("smartrecruiters", _detect_smartrecruiters, 1),
    ("workable",        _detect_workable,        1),
    ("eightfold",       _detect_eightfold,       1),
    ("workday",         _detect_workday,         1),
    ("pinpoint",        _detect_pinpoint,        1),
    ("webitrent",       _detect_webitrent,       1),
    ("ciphr",           _detect_ciphr,           1),
]


# Known marketing-page hosts that hide a standard board underneath. When the
# pasted URL is the pretty front-end, we can't see the real board from the URL
# alone - but we can tell the user where to look. (Proven examples from testing.)
_MARKETING_HINTS = {
    "anthropic.com": "Anthropic's real board is Greenhouse - try the URL "
                     "'https://job-boards.greenhouse.io/anthropic'.",
    "lovable.dev":   "Lovable's jobs are on Ashby or Greenhouse - open the "
                     "careers page, click a job, and copy the URL it lands on.",
}


def _derive_key(provider: str, config: dict, host: str) -> str:
    """Build a sensible internal key from whatever identity we extracted."""
    for field in ("board", "company", "account", "tenant", "subdomain"):
        if config.get(field):
            return re.sub(r"[^a-z0-9-]", "", config[field].lower())
    # Fall back to the host's first label (e.g. "wellcome" from the host).
    label = host.split(".")[0]
    return re.sub(r"[^a-z0-9-]", "", label.lower())


def detect(url: str) -> dict:
    """
    Inspect a pasted careers URL and return a detection result (see module docs).
    Never raises for an unknown site - it returns a tier-3, provider=None result
    with a clear message, because "we can't auto-add this" is a valid answer.
    """
    url = (url or "").strip()
    if not url:
        return {
            "provider": None, "config": {}, "suggested_key": "", "display_guess": "",
            "tier": 3, "confident": False,
            "message": "No URL given. Paste the careers page address.",
        }

    parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parsed.query or ""

    # Apple / Google are Tier-2 presets, handled by name elsewhere, but if a
    # user pastes their careers URL we can recognise and point them at the preset.
    if "apple.com" in host and "jobs" in (host + path):
        return {
            "provider": "apple", "config": {}, "suggested_key": "apple",
            "display_guess": "Apple", "tier": 2, "confident": True,
            "message": "Recognised Apple - this is a built-in preset, just pick it.",
        }
    if "google.com" in host and "careers" in path:
        return {
            "provider": "google", "config": {}, "suggested_key": "google",
            "display_guess": "Google", "tier": 2, "confident": True,
            "message": "Recognised Google - this is a built-in preset, just pick it.",
        }
    # Uber (Phase O Part 2) — a single-company custom board on jobs.uber.com.
    # No token to extract; the connector reads your chosen cities and resolves
    # coordinates itself. Recognised by host so a pasted careers URL just works.
    if "uber.com" in host and ("jobs" in host or "careers" in path or "jobs" in path):
        return {
            "provider": "uber", "config": {}, "suggested_key": "uber",
            "display_guess": "Uber", "tier": 1, "confident": True,
            "message": "Recognised Uber Careers - a built-in connector, just add it.",
        }
    # Spotify (Phase O Part 2) — lifeatspotify.com, single-company custom board.
    if "lifeatspotify.com" in host:
        return {
            "provider": "spotify", "config": {}, "suggested_key": "spotify",
            "display_guess": "Spotify", "tier": 1, "confident": True,
            "message": "Recognised Spotify Careers - a built-in connector, just add it.",
        }
    # PlayStation — careers.playstation.com, custom API across studio Greenhouse
    # boards. No token to extract; the connector reads chosen cities itself.
    if "careers.playstation.com" in host or ("playstation.com" in host and "careers" in (host + path)):
        return {
            "provider": "playstation", "config": {}, "suggested_key": "playstation",
            "display_guess": "PlayStation", "tier": 1, "confident": True,
            "message": "Recognised PlayStation Careers - a built-in connector, just add it.",
        }
    # Deliveroo — careers.deliveroo.co.uk, a WordPress REST careers board.
    if "careers.deliveroo.co.uk" in host or ("deliveroo." in host and "careers" in (host + path)):
        return {
            "provider": "deliveroo", "config": {}, "suggested_key": "deliveroo",
            "display_guess": "Deliveroo", "tier": 1, "confident": True,
            "message": "Recognised Deliveroo Careers - a built-in connector, just add it.",
        }
    # Victoria and Albert Museum — vam.ac.uk/vacancies, server-rendered HTML list.
    if "vam.ac.uk" in host and "vacanc" in (path or ""):
        return {
            "provider": "va", "config": {}, "suggested_key": "va",
            "display_guess": "V&A", "tier": 1, "confident": True,
            "message": "Recognised V&A vacancies - a built-in connector, just add it.",
        }
    # Soho House — careers.sohohouse.com, a Next.js single-company custom board.
    if "careers.sohohouse.com" in host or ("sohohouse.com" in host and "careers" in (host + path)):
        return {
            "provider": "sohohouse", "config": {}, "suggested_key": "sohohouse",
            "display_guess": "Soho House", "tier": 1, "confident": True,
            "message": "Recognised Soho House Careers - a built-in connector, just add it.",
        }

    # Try each recogniser.
    for provider, fn, tier in _RECOGNISERS:
        config = fn(url, host, path, query)
        if config is None:
            continue
        runnable = provider in CONNECTORS
        key = _derive_key(provider, config, host)
        # display guess: prettify the key
        display = key.replace("-", " ").title() if key else ""
        if runnable:
            return {
                "provider": provider, "config": config,
                "suggested_key": key, "display_guess": display,
                "tier": tier, "confident": bool(key),
                "message": (f"Detected {provider}." +
                            ("" if key else
                             " Couldn't derive a short name from the URL - "
                             "you'll need to give it one.")),
            }
        # Recognised but connector not built yet.
        return {
            "provider": provider, "config": config,
            "suggested_key": key, "display_guess": display,
            "tier": 3, "confident": False,
            "message": (f"This looks like {provider}, which JobWatch recognises "
                        f"but doesn't have a working connector for yet. Recorded "
                        f"so it can be built."),
        }

    # No recogniser matched. Check for a known marketing-page hint.
    for marketing_host, hint in _MARKETING_HINTS.items():
        if marketing_host in host:
            return {
                "provider": None, "config": {}, "suggested_key": "",
                "display_guess": "", "tier": 3, "confident": False,
                "message": ("This is a careers landing page, not the job board "
                            "itself. " + hint),
            }

    # Genuinely unrecognised - honest Tier 3.
    return {
        "provider": None, "config": {}, "suggested_key": "", "display_guess": "",
        "tier": 3, "confident": False,
        "message": ("Couldn't identify a supported job board from this URL. It "
                    "may be a custom site that needs a connector built (a short "
                    "collaboration). The URL has been noted as a request."),
    }


# Quick manual test:  python3 -m jobwatch.detect "<url>"
if __name__ == "__main__":
    import sys
    test_urls = sys.argv[1:] or [
        "https://job-boards.greenhouse.io/anthropic",
        "https://jobs.lever.co/mistral?location=London",
        "https://jobs.ashbyhq.com/strava",
        "https://wellcome.wd3.myworkdayjobs.com/en-GB/Wellcome",
        "https://harpercollins.pinpointhq.com/opportunities",
        "https://www.figma.com/careers/",
    ]
    for u in test_urls:
        r = detect(u)
        print(f"\n{u}")
        print(f"  provider={r['provider']} tier={r['tier']} key={r['suggested_key']!r} "
              f"confident={r['confident']}")
        print(f"  {r['message']}")
