"""Accessibility regression guard for the presentation layer.

    python3 -m tests.test_contrast

Why this exists
---------------
The 2026-07-26 audit found that three "FIXED, never themed" tokens and one
double-duty accent token failed WCAG 2.2 AA contrast on half the grounds they
were drawn on. Those values are now correct. This test is what keeps them
correct: the failure mode is not "someone reverts the fix", it is "someone adds
a seventh palette, or a new `color: var(--accent)` rule, and never re-measures".

It reads the REAL stylesheet — no fixture, no copy — resolves the cascade the
way a browser would, and measures every pairing that ends up on screen.

Three kinds of check
--------------------
1. CONTRAST   every token drawn as text, against every ground it lands on.
2. COMPLETENESS  every theme block defines every themeable channel. A missing
   channel is invisible: the value silently falls back to :root and reads as
   the wrong palette rather than erroring.
3. STATIC LINT  no new text usage of a ring-only token; the JW-04 live regions
   still exist. These are cheap greps, not a substitute for a screen reader.

Stdlib only, in keeping with the rest of the project.
"""

import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Locate the presentation layer. tests/ sits at the repo root.
# --------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "jobwatch" / "web"
CSS_PATH = WEB / "app.css"
JS_PATH = WEB / "app.js"
HTML_PATH = WEB / "index.html"

AA_TEXT = 4.5      # SC 1.4.3, text below 18pt / 14pt bold
AA_LARGE = 3.0     # SC 1.4.3, large text
AA_NONTEXT = 3.0   # SC 1.4.11, UI component boundaries and graphics

# A small buffer over the threshold. Contrast tools disagree in the second
# decimal place because of rounding in the sRGB->linear step; a value sitting
# at exactly 4.50 in one tool reads 4.49 in another and the audit gets argued
# about rather than trusted. Everything here is authored to clear 4.60.
BUFFER = 0.10


# --------------------------------------------------------------------------
# Colour maths — WCAG 2.x relative luminance and contrast ratio.
# --------------------------------------------------------------------------

def _srgb_to_linear(channel):
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    if len(h) == 3:                       # #abc -> #aabbcc
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError(f"not a hex colour: {hex_colour!r}")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * _srgb_to_linear(r)
            + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b))


def contrast(fg, bg):
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------
# Parse the token blocks and resolve the cascade.
# --------------------------------------------------------------------------

DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

THEME_SELECTORS = {
    "active/light": ':root[data-theme="active"] {',
    "active/dark": ':root[data-theme="active"][data-mode="dark"] {',
    "casual/light": ':root[data-theme="casual"] {',
    "casual/dark": ':root[data-theme="casual"][data-mode="dark"] {',
    "dormant/light": ':root[data-theme="dormant"] {',
    "dormant/dark": ':root[data-theme="dormant"][data-mode="dark"] {',
}

# Channels every theme block must repoint. Adding a themeable colour means
# adding it here too — that is the point of the completeness check.
REQUIRED_CHANNELS = (
    "--paper", "--paper-2", "--ink", "--ink-soft", "--ink-faint",
    "--rule", "--rule-2", "--accent", "--accent-bg", "--on-accent",
    "--accent-text", "--control-border",
)


def blank_comments(css):
    """Blank every comment's CONTENT, keeping the file's shape.

    Comments are replaced character-for-character with spaces, newlines left
    alone. So the text stops matching anything, but line numbers and offsets
    are identical to the original — which matters, because the lints report
    "app.css:1991" and that number has to be real.

    Both halves of this file need it. The token parser needs it because a
    comment mentioning "--some-token:" followed by prose gets matched as a
    declaration and swallows the next real one, silently dropping it from the
    palette. The lints need it because a comment that merely MENTIONS
    `outline: none` or `color: var(--accent)` — which any honest note about
    JW-05 or JW-03 will — gets reported as the very violation it describes.

    Both were live bugs, found the same way: the guard failing on its own
    subject matter.
    """
    return COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), css)


def _block(css, selector):
    """Token declarations inside one block, comments stripped first.

    A comment that mentions "--some-token:" and then runs on in prose is
    otherwise matched as a declaration, and it swallows everything up to
    the next semicolon — which is a REAL declaration that then silently
    vanishes from the palette. Not hypothetical: it happened while writing
    the JW-12 comment, and the symptom was "--slate is undefined" rather
    than anything pointing at a comment.
    """
    start = css.index(selector)
    end = css.index("}", start)
    return dict(DECL.findall(blank_comments(css[start:end])))


def _deref(value, scope):
    """Resolve `var(--x)` one hop, which is all this stylesheet uses."""
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    return scope.get(m.group(1), value).strip() if m else value.strip()


def load_palettes(css):
    """Return {palette name: {token: literal hex}} with the cascade applied.

    Order matters and mirrors the stylesheet: :root, then the mode-wide dark
    block (danger + the semantic flags), then the theme block.
    """
    root = _block(css, ":root {")
    dark_mode = _block(css, ':root[data-mode="dark"] {')

    palettes = {}

    # The baseline is a real rendering state, not just an inheritance source:
    # it is what shows before data-theme lands, or if the JS never sets it.
    # JW-06 was exactly this case, so it gets tested like any other palette.
    palettes["baseline"] = dict(root)

    for name, selector in THEME_SELECTORS.items():
        scope = dict(root)
        if "dark" in name:
            scope.update(dark_mode)
        scope.update(_block(css, selector))
        palettes[name] = scope

    for name, scope in palettes.items():
        palettes[name] = {k: _deref(v, scope) for k, v in scope.items()}

    return palettes


# --------------------------------------------------------------------------
# What is drawn on what.
#
# Each entry: (foreground token, background token, threshold, why).
# "why" names the rule so a failure points at a line rather than a colour.
# --------------------------------------------------------------------------

PAIRS = [
    # --- JW-02 -----------------------------------------------------------
    ("--ink-faint", "--paper", AA_TEXT, "captions and meta, 63 usages at 11-13.5px"),
    ("--ink-faint", "--paper-2", AA_TEXT, "same, on raised cards"),

    # --- JW-03. The third ground is the one the audit missed: the pills and
    # chips draw accent type on accent wash, not on paper. -----------------
    ("--accent-text", "--paper", AA_TEXT, "a{}, .mc-clear, .pick-count, links"),
    ("--accent-text", "--paper-2", AA_TEXT, "same, on raised cards"),
    ("--accent-text", "--accent-bg", AA_TEXT, ".tag.new/.interest/.bucket, .tab-badge, .chip, .dc-count"),

    # --- JW-06 -----------------------------------------------------------
    ("--on-accent", "--accent", AA_TEXT, "text on an accent fill, incl. .dept-chip.on"),

    # --- JW-01. --removed is drawn on paper; --stretch and --ok are only
    # ever drawn on their own pill grounds, which is why those grounds are
    # tokens now and move with them. -------------------------------------
    ("--removed", "--paper", AA_TEXT, ".tr-gone"),
    ("--removed", "--paper-2", AA_TEXT, ".job.is-removed .j-title"),
    ("--stretch", "--stretch-bg", AA_TEXT, ".tag.stretch"),
    ("--ok", "--paper", AA_TEXT, ".tick.done"),
    ("--ok", "--ok-bg", AA_TEXT, ".cl-status.ok"),

    # --- Not audited, currently passing. Here so a palette edit cannot
    # break them unnoticed. ----------------------------------------------
    ("--ink", "--paper", AA_TEXT, "body text"),
    ("--ink", "--paper-2", AA_TEXT, "body text on cards"),
    ("--ink-soft", "--paper", AA_TEXT, "secondary text"),
    ("--ink-soft", "--paper-2", AA_TEXT, "secondary text on cards"),
    ("--danger", "--paper", AA_TEXT, ".btn.ghost.danger"),
    ("--danger", "--paper-2", AA_TEXT, ".btn.ghost.danger on cards"),

    # --- Recorded gap, measured so it stays visible. ---------------------
    ("--slate", "--paper", AA_TEXT, ".job .j-company on every job row"),
    ("--slate", "--paper-2", AA_TEXT, ".job .j-company on cards"),
    ("--slate", "--slate-bg", AA_TEXT, ".tag.unclear, .cl-status.pending, .detect-msg.warn"),

    # --- Non-text: rings, fills, borders. 3:1 under SC 1.4.11. -----------
    ("--accent", "--paper", AA_NONTEXT, "focus ring, .rail-item.on border"),
    ("--accent", "--paper-2", AA_NONTEXT, "focus ring on cards"),

    # --- JW-07: the sole boundary of a transparent control ---------------
    ("--control-border", "--paper", AA_NONTEXT, ".btn.ghost boundary"),
    ("--control-border", "--paper-2", AA_NONTEXT, ".btn.ghost on cards"),

    # --- JW-13/JW-14: the two-band focus ring -----------------------------
    # The bands must contrast with EACH OTHER — that is what makes the
    # indicator ground-independent. Grounds themselves are checked by
    # check_focus_ring() below, which needs the max() rule these pairs cannot
    # express.
    ("--focus-ring", "--focus-halo", AA_NONTEXT, "the two bands against each other"),
]

# Failures that are real, understood, and deliberately not yet fixed. Each one
# is reported every run so it cannot be forgotten, but does not fail the build.
# Move an entry out of here the moment it is fixed — an empty dict is the goal.
KNOWN_GAPS = {
    ("--accent", "--paper-2"): (
        "JW-13, residual. The FOCUS ring no longer uses --accent — it is now "
        "two-band (see --focus-ring). What still reads --accent at 2.78:1 on "
        "cards is state-indicator borders: .rail-item.on's left edge and "
        ".jobs-tab.on's underline. Lower stakes than focus, because each also "
        "carries a background or weight change, but it is still an SC 1.4.11 "
        "shortfall and the fix is the same decision as before: darken the "
        "active/light amber, which moves the app's signature colour."
    ),
}


# --------------------------------------------------------------------------
# Static lints. Cheap, and they catch the regressions a colour check cannot.
# --------------------------------------------------------------------------

# --accent as type must go through --accent-text. These are the rules where
# --accent is legitimately still a colour: SVG icon glyphs, which are non-text
# and need 3:1, not 4.5:1. Anything else showing up here is a new failure.
ACCENT_AS_COLOUR_ALLOWED = {
    ".switch-trigger.is-active  .glyph { color: var(--accent); }",
    ".switch-item.is-active  .glyph { color: var(--accent); }",
    ".star:hover { color: var(--accent); }",
    ".star.is-on { color: var(--accent); }",
}

# JW-05. `outline: none` is only defensible when the same rule supplies an
# equally visible replacement, which in practice nobody remembers to do —
# all five occurrences in this file were either bare or replaced by a
# border-colour change, which does not meet 2.4.13 Focus Appearance. The
# allowlist is empty on purpose.
OUTLINE_NONE_ALLOWED = set()

# JW-13/JW-14. A focus rule must use --focus-ring, never --accent directly.
# --accent is a mid-tone: it was 2.78:1 on cards, and 1.00:1 where the element
# it outlined was itself filled with --accent.
FOCUS_RULE = re.compile(r"outline\s*:\s*[\d.]+px\s+solid\s+var\((--[\w-]+)\)")

JS_REQUIRED = [
    ("function announce(", "the JW-04 announcement helper"),
    ("function runSummary(", "the shared run-summary sentence"),
    ("announce(runSummary(", "completion is announced on at least one run path"),
]

HTML_REQUIRED = [
    ('id="srStatus"', "the polite live region"),
    ('id="srAlert"', "the assertive live region"),
    ('aria-live="polite"', "polite politeness setting"),
    ('aria-live="assertive"', "assertive politeness setting"),
]


def lint_css(css):
    problems = []

    # Every check below scans the blanked copy. Line numbers are preserved, so
    # reported positions still point at the real file.
    scannable = blank_comments(css)

    # 1. No new text usage of the ring-only token.
    for i, line in enumerate(scannable.splitlines(), start=1):
        stripped = line.strip()
        if re.search(r"(?<!-)\b(?:color|fill)\s*:\s*var\(--accent\)\s*;", stripped):
            if stripped not in ACCENT_AS_COLOUR_ALLOWED:
                problems.append(
                    f"app.css:{i} draws --accent as type: {stripped!r}\n"
                    f"        Use --accent-text. --accent is authored for 3:1 "
                    f"(rings, fills, borders) and fails 4.5:1 as type in every "
                    f"light palette. If this really is an icon, add it to "
                    f"ACCENT_AS_COLOUR_ALLOWED with a note."
                )

    # 2. Every theme block repoints every themeable channel.
    for name, selector in THEME_SELECTORS.items():
        declared = _block(css, selector)
        missing = [c for c in REQUIRED_CHANNELS if c not in declared]
        if missing:
            problems.append(
                f"theme block {name} does not set {', '.join(missing)}.\n"
                f"        It will silently inherit the baseline and render as "
                f"the wrong palette rather than erroring."
            )

    # 3. Focus rings go through the two-band tokens.
    for i, line in enumerate(scannable.splitlines(), start=1):
        for tok in FOCUS_RULE.findall(line):
            if tok != "--focus-ring":
                problems.append(
                    f"app.css:{i} draws a focus ring with {tok}.\n"
                    f"        Use var(--focus-ring) with a var(--focus-halo) "
                    f"box-shadow. A single-band ring cannot be visible on every "
                    f"ground: five rules inset the ring onto the element's own "
                    f"fill, and one of those fills IS --accent."
                )

    # 4. No suppressed focus indicators.
    for i, line in enumerate(scannable.splitlines(), start=1):
        stripped = line.strip()
        if re.search(r"outline\s*:\s*none", stripped):
            if stripped not in OUTLINE_NONE_ALLOWED:
                problems.append(
                    f"app.css:{i} suppresses the focus indicator: {stripped!r}\n"
                    f"        SC 2.4.7 needs a visible one. Use the pattern "
                    f"the rest of this file already uses: outline: 2.5px "
                    f"solid var(--accent); outline-offset: 2px. A "
                    f"border-colour change alone does not meet 2.4.13."
                )

    # 5. The sr-only utility must not be display:none — that removes the node
    #    from the accessibility tree and the live regions announce nothing.
    m = re.search(r"\.sr-only\s*\{([^}]*)\}", scannable)
    if not m:
        problems.append("app.css has no .sr-only rule; the JW-04 live regions are unstyled.")
    elif re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", m.group(1)):
        problems.append(
            ".sr-only uses display:none or visibility:hidden.\n"
            "        Both remove the node from the accessibility tree, so the "
            "live regions will never announce. Use the clip/clip-path pattern."
        )

    return problems


def lint_js_and_html(js, html):
    problems = []
    for needle, why in JS_REQUIRED:
        if needle not in js:
            problems.append(f"app.js is missing {needle!r} — {why}.")
    for needle, why in HTML_REQUIRED:
        if needle not in html:
            problems.append(f"index.html is missing {needle!r} — {why}.")

    # The live regions must sit outside #view. Every screen is drawn with
    # replaceChildren() on #view, and a live region that gets torn down and
    # rebuilt never fires.
    view_pos = html.find('id="view"')
    status_pos = html.find('id="srStatus"')
    if view_pos != -1 and status_pos != -1:
        tail = html[view_pos:status_pos]
        if "</main>" not in tail:
            problems.append(
                "The live regions appear to be inside <main id=\"view\">.\n"
                "        #view is replaced wholesale on every render, which "
                "destroys the regions and silences them. Keep them in the shell."
            )
    return problems


# --------------------------------------------------------------------------
# Run.
# --------------------------------------------------------------------------

# Every ground a focus ring can land on. Outset rings sit on the parent's
# background; the five inset rings sit on the element's OWN fill, which is why
# --ink and --accent appear here. The indicator passes if EITHER band clears
# 3:1 — that is the whole point of having two.
RING_GROUNDS = [
    ("--paper", "outset rings, on the page"),
    ("--paper-2", "outset rings, on cards and panels"),
    ("--ink", ".toggle button.on — a dark fill in light mode"),
    ("--accent", ".appearance-seg button.on — was 1.00:1 with a single-band ring"),
    ("--accent-bg", ".rail-item.on, .pick-company.is-on"),
]


def check_focus_ring(palettes):
    """A two-band ring passes a ground if either band clears 3:1 against it.

    A plain pair check cannot express this: each band fails some ground on its
    own, and that is fine and expected. What matters is that they never fail
    the same one.
    """
    failures = []
    for palette, tokens in sorted(palettes.items()):
        for ground, why in RING_GROUNDS:
            if not all(t in tokens for t in ("--focus-ring", "--focus-halo", ground)):
                failures.append(f"{palette}: cannot measure the ring against {ground}.")
                continue
            best = max(contrast(tokens["--focus-ring"], tokens[ground]),
                       contrast(tokens["--focus-halo"], tokens[ground]))
            if best < AA_NONTEXT:
                failures.append(
                    f"{palette}: NEITHER focus band clears 3:1 against {ground} "
                    f"(best {best:.2f}:1) — {why}. The focus indicator is "
                    f"invisible there."
                )
    return failures


def check_contrast(palettes):
    failures, gaps, rows = [], [], []

    for palette, tokens in sorted(palettes.items()):
        for fg, bg, threshold, why in PAIRS:
            if fg not in tokens or bg not in tokens:
                failures.append(
                    f"{palette}: {fg} or {bg} is undefined; cannot measure ({why})."
                )
                continue
            ratio = contrast(tokens[fg], tokens[bg])
            # The buffer applies only to text pairings, whose values were
            # authored against it. The non-text values are inherited design
            # decisions, so hold them to the bare standard rather than
            # generating noise about colours nobody chose for this purpose.
            need = threshold + (BUFFER if threshold == AA_TEXT else 0.0)
            ok = ratio >= need
            rows.append((palette, fg, bg, tokens[fg], tokens[bg], ratio, threshold, ok))
            if not ok:
                entry = (
                    f"{palette}: {fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) "
                    f"= {ratio:.2f}:1, needs {threshold}:1 — {why}"
                )
                if (fg, bg) in KNOWN_GAPS:
                    gaps.append(((fg, bg), entry))
                else:
                    failures.append(entry)

    return failures, gaps, rows


def main(argv=None):
    argv = argv or sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv

    for path in (CSS_PATH, JS_PATH, HTML_PATH):
        if not path.exists():
            print(f"FAIL  cannot find {path}")
            return 1

    css = CSS_PATH.read_text(encoding="utf-8")
    js = JS_PATH.read_text(encoding="utf-8")
    html = HTML_PATH.read_text(encoding="utf-8")

    palettes = load_palettes(css)
    failures, gaps, rows = check_contrast(palettes)
    failures += check_focus_ring(palettes)
    problems = lint_css(css) + lint_js_and_html(js, html)

    if verbose:
        print(f"{'palette':14} {'foreground':14} {'ground':13} {'ratio':>7}  need")
        print("-" * 60)
        for palette, fg, bg, _fgv, _bgv, ratio, threshold, ok in rows:
            mark = " " if ok else "<"
            print(f"{palette:14} {fg:14} {bg:13} {ratio:>6.2f}:1 {threshold:>5} {mark}")
        print()

    print(f"{len(palettes)} palettes, {len(rows)} contrast pairings measured, "
          f"{len(palettes) * len(RING_GROUNDS)} focus-ring grounds checked.")

    if gaps:
        print(f"\n{len(gaps)} KNOWN GAP(S) — recorded, not yet fixed:")
        for key, entry in gaps:
            print(f"  · {entry}")
            print(f"    {KNOWN_GAPS[key]}")

    if not failures and not problems:
        print("\nPASS  every measured pairing clears its threshold; lints clean.")
        return 0

    if failures:
        print(f"\n{len(failures)} CONTRAST FAILURE(S):")
        for f in failures:
            print(f"  · {f}")
    if problems:
        print(f"\n{len(problems)} LINT PROBLEM(S):")
        for p in problems:
            print(f"  · {p}")

    print("\nFAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
