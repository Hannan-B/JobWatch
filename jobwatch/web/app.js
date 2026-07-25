/* ===========================================================================
   JobWatch interface logic — Phase E (Batch 1)
   Three things live here:
     1. HOME: the phase-state reading + the menu (pick a bucket / start a phase)
     2. RUN:  a live progress bar fed by the engine's SSE events
     3. REPORT: the finished run, shown two ways (per-company / all roles)
   It talks to the local server's small JSON + SSE API. No framework, no build.
   =========================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
};

const api = {
  async get(path) {
    const r = await fetch(path);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  },
};

function toast(message, isError = false) {
  const t = el('div', { class: 'toast' + (isError ? ' err' : '') }, message);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), isError ? 5200 : 2800);
}

/* Phase O — gently warn when a just-added location isn't a city geo recognises.
   The location is ALWAYS kept (we never block adding one). If geo can't resolve
   the city to a fetch region, the big boards (Apple, Google) can't be scoped to
   it, so they fetch everything that run and the filter narrows afterwards — still
   correct, just slower. We tell the user that, once, as a non-error toast. This
   is fire-and-forget: it never delays the chip appearing. */
async function warnIfCityUnrecognised(city) {
  const v = (city || '').trim();
  if (!v) return;
  try {
    const r = await api.get('/api/geo/check?city=' + encodeURIComponent(v));
    if (r && r.recognised === false) {
      toast(`“${v}” isn’t a city we recognise, so Apple and Google will fetch ` +
            `everything and filter to it (slower) rather than fetching just its ` +
            `region. It still works.`);
    }
  } catch (e) {
    /* Never let the check disrupt anything — silently ignore. */
  }
}

/* ---- state held in memory only (no browser storage) ------------------- */
let HOME = null;          // last /api/home payload
let LAST_RESULT = null;   // last finished run result (this session)
let REPORT_VIEW = 'company'; // 'company' | 'all'
let CURRENT_ROUTE = 'home';  // the route name we're showing
let COLOR_MODE = 'system';   // 'light' | 'dark' | 'system' — the user's choice (persisted server-side)

/* =======================================================================
   THEME (Phase H) — phase-type colour theming × light/dark.
   Two attributes on <html> drive the whole re-skin via CSS:
     data-theme = the phase type (active | casual | dormant), from /api/home
     data-mode  = the RESOLVED polarity (light | dark)
   COLOR_MODE is the user's preference; "system" follows the OS and the time of
   day via prefers-color-scheme, with a live listener so a dusk flip repaints
   without a reload. The choice is persisted server-side (GET/POST /api/ui-prefs,
   a sibling data file — transport only, no engine-logic change, per §V2.5).
   ======================================================================= */

const _darkMQ = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

/* Resolve COLOR_MODE ("system") down to the actual light|dark to apply. */
function resolvedMode() {
  if (COLOR_MODE === 'light' || COLOR_MODE === 'dark') return COLOR_MODE;
  return (_darkMQ && _darkMQ.matches) ? 'dark' : 'light';   // system
}

/* Set both attributes on <html>. Phase type comes from HOME.phase_state; before
   HOME loads we default to dormant (the calmest, safest neutral). */
function applyTheme() {
  const root = document.documentElement;
  const theme = (HOME && HOME.phase_state) ? HOME.phase_state : 'dormant';
  root.setAttribute('data-theme', theme);
  root.setAttribute('data-mode', resolvedMode());
}

/* When the OS appearance changes (e.g. at dusk) AND the user is on "system",
   repaint live. Registered once at boot. */
function wireSystemModeListener() {
  if (!_darkMQ) return;
  const onChange = () => { if (COLOR_MODE === 'system') applyTheme(); };
  if (_darkMQ.addEventListener) _darkMQ.addEventListener('change', onChange);
  else if (_darkMQ.addListener) _darkMQ.addListener(onChange);   // older Safari
}

/* Load the saved appearance preference once at boot. Forgiving: any failure
   leaves COLOR_MODE at its 'system' default (the app must theme regardless). */
async function loadColorMode() {
  try {
    const r = await api.get('/api/ui-prefs');
    const m = r && r.color_mode;
    if (m === 'light' || m === 'dark' || m === 'system') COLOR_MODE = m;
  } catch (e) { /* keep the default */ }
}

/* Change the appearance preference, apply it, and persist it. Optimistic: we
   repaint immediately, then save; a save failure just toasts (the look still
   changed for this session). */
async function setColorMode(mode) {
  COLOR_MODE = mode;
  applyTheme();
  try {
    await api.post('/api/ui-prefs', { color_mode: mode });
  } catch (e) { toast('Couldn’t save your appearance choice.', true); }
}


/* =======================================================================
   ROUTER (G.2) — a tiny hash router, no library.
   Each route name maps to a render function. We reuse the existing v1 screen
   functions wherever we can; the genuinely-new screens in G are the banner,
   this router, the conditional Home, and the phase switcher.

   The banner shows by default on every route EXCEPT home, where it hover/
   focus-reveals (G.1). Changing the hash (or calling go()) re-renders.
   ======================================================================= */

const ROUTES = {
  home:      renderHome,        // NEW conditional Home (G.3)
  jobs:      openJobs,          // Jobs — run a check + browse roles + saved (Phase K)
  tracker:   renderTracker,     // Application Tracker (Phase L)
  companies: openManage,        // today's manage screen (Phase I overhauls it)
  trends:    openTrends,        // today's trends screen
  setup:     renderGuide,       // Guide — the Tier-3 field guide (Phase J)
  history:   openHistory,       // History — past phases, browsable (Phase J)
  settings:  openSettings,      // today's settings (interests + guide grouped in)
  report:    openLastReport,    // the last run's report, revisitable (G.9)
};

/* Navigate to a route. Updates the hash so refresh/back behave (G.2). */
function go(name) {
  const target = ROUTES[name] ? name : 'home';
  if (location.hash !== '#' + target) {
    location.hash = '#' + target; // triggers hashchange -> render()
  } else {
    render(); // same route (e.g. a refresh-in-place) — render anyway
  }
}

/* Render whatever the current hash points at. */
async function render() {
  const name = (location.hash || '#home').slice(1).split('?')[0] || 'home';
  CURRENT_ROUTE = ROUTES[name] ? name : 'home';

  // Home needs fresh /api/home every visit (phase may have changed); other
  // routes that need it pull their own data. Make sure HOME is populated so
  // the banner's switcher is correct everywhere.
  if (!HOME || CURRENT_ROUTE === 'home') {
    try {
      HOME = await api.get('/api/home');
    } catch (e) {
      applyTheme();   // theme even the error screen (defaults to dormant)
      $('#reading').replaceChildren();
      $('#view').replaceChildren(errorBox('Couldn’t load your JobWatch data.', e.message));
      renderBanner();
      return;
    }
  }

  applyTheme();   // (H) re-skin per the current phase type + resolved mode
  renderBanner();
  applyBannerVisibility();

  // The reading spine (phase name · cadence · last-checked) is suppressed
  // app-wide (Phase I) — it was useful context on the old single-page app but
  // reads as clutter on the real screens. It returns ONLY on the Jobs page
  // (Phase K), where you're about to run a check and that context matters.
  // Home draws its own hero and never wants the spine. So: clear #reading on
  // every route, and only draw it on the (future) 'jobs' route.
  if (CURRENT_ROUTE === 'jobs' && HOME) {
    renderReading();
  } else {
    $('#reading').replaceChildren();
  }

  ROUTES[CURRENT_ROUTE]();
}

/* Back-compat: a lot of v1 screens call loadHome() to return home. Keep the
   name, but route through the router so the hash and banner stay in sync. */
function loadHome() { go('home'); }

/* =======================================================================
   BANNER (G.1) — one element, every page. Layout: phase switcher (top-left) ·
   nav destinations (centre) · settings cog (right). The banner spans the full
   width; its nav is centred.
   ======================================================================= */

function renderBanner() {
  const banner = $('#banner');

  // Centre: nav destinations, in the locked order (Jobs first, then tracker).
  const navItems = [
    ['jobs',      'Jobs'],
    ['tracker',   'Application Tracker'],
    ['companies', 'Companies & Buckets'],
    ['trends',    'Trends'],
    ['setup',     'Guide'],
    ['history',   'History'],
  ];
  const nav = el('div', { class: 'banner-nav' },
    navItems.map(([name, label]) =>
      el('button', {
        class: 'banner-link' + (CURRENT_ROUTE === name ? ' on' : ''),
        // History from the banner always means "the list" — clear any drill-in.
        onclick: () => { if (name === 'history') { HISTORY_PHASE = null; HISTORY_CONFIRM_DELETE = false; HISTORY_ROW_CONFIRM = null; } go(name); },
      }, label))
  );

  // Right: the settings cog. Always reachable.
  const settingsBtn = el('button', {
    class: 'banner-icon' + (CURRENT_ROUTE === 'settings' ? ' on' : ''),
    title: 'Settings', 'aria-label': 'Settings', onclick: () => go('settings'),
  }, [cogGlyph()]);

  banner.replaceChildren(
    renderSwitcher(),   // top-left phase dropdown
    nav,                // centred destinations
    settingsBtn,        // far-right cog
  );
}

/* Show the banner everywhere except Home; on Home it's hidden and revealed by
   hovering or focusing the top edge (G.1). The edge affordance only exists on
   Home. */
function applyBannerVisibility() {
  const banner = $('#banner');
  const edge = $('#bannerEdge');
  if (CURRENT_ROUTE === 'home') {
    banner.hidden = true;
    banner.classList.remove('is-static');
    banner.classList.add('is-revealable');
    edge.hidden = false;
    document.body.classList.add('on-home');
  } else {
    banner.hidden = false;
    banner.classList.add('is-static');
    banner.classList.remove('is-revealable', 'is-open');
    edge.hidden = true;
    document.body.classList.remove('on-home');
  }
}

/* Hover/focus reveal wiring for the Home banner (set up once at boot). */
function wireBannerReveal() {
  const banner = $('#banner');
  const edge = $('#bannerEdge');
  const open = () => { if (CURRENT_ROUTE === 'home') { banner.hidden = false; banner.classList.add('is-open'); } };
  const close = () => { if (CURRENT_ROUTE === 'home') { banner.classList.remove('is-open'); banner.hidden = true; } };
  edge.addEventListener('mouseenter', open);
  edge.addEventListener('focus', open);
  banner.addEventListener('mouseenter', open);
  banner.addEventListener('mouseleave', close);
  // Keyboard: focus leaving the banner entirely closes it on Home.
  banner.addEventListener('focusout', (e) => {
    if (CURRENT_ROUTE === 'home' && !banner.contains(e.relatedTarget)) close();
  });
}

/* =======================================================================
   PHASE SWITCHER (G.4) — a dropdown at top-left. The trigger shows the CURRENT
   state as glyph + name; hovering (or focusing/clicking) reveals a menu of the
   three states with a smooth transition. Picking a DIFFERENT state asks to
   confirm, then calls the existing engine endpoints (no engine change):
   create_phase / switch_type / end_phase. The engine persists the result, so
   the state is remembered next open automatically.
   ======================================================================= */

const PHASE_STATES = [
  ['active',  'Active'],
  ['casual',  'Casual'],
  ['dormant', 'Dormant'],
];

const STATE_LABEL = { active: 'Active', casual: 'Casual', dormant: 'Dormant' };

function renderSwitcher() {
  const current = HOME ? HOME.phase_state : 'dormant';

  // The always-visible trigger: current state's glyph + name + a small caret.
  const trigger = el('button', {
    class: 'switch-trigger is-' + current,
    'aria-haspopup': 'true', 'aria-expanded': 'false',
    title: 'Change phase',
  }, [
    stateGlyph(current),
    el('span', { class: 'switch-label' }, STATE_LABEL[current]),
    caretGlyph(),
  ]);

  // The menu: one row per state, current marked (highlighted, no "now" tag).
  const menu = el('div', { class: 'switch-menu', role: 'menu' },
    PHASE_STATES.map(([state, label]) => {
      const on = current === state;
      return el('button', {
        class: 'switch-item is-' + state + (on ? ' on' : ''),
        role: 'menuitem',
        'aria-current': on ? 'true' : null,
        title: switchTitle(state, on),
        onclick: () => { closeSwitcher(drop); onSwitchTap(state); },
      }, [
        stateGlyph(state),
        el('span', { class: 'switch-label' }, label),
      ]);
    })
  );

  const drop = el('div', { class: 'switcher', 'aria-label': 'Phase state' }, [trigger, menu]);

  // Open on hover/focus/click. Closing uses a GRACE DELAY so the menu doesn't
  // snap shut the instant the cursor wobbles or crosses the small gap — it stays
  // put until the cursor has been away for a beat. Re-entering cancels the close.
  let closeTimer = null;
  const cancelClose = () => { if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; } };
  const open = () => { cancelClose(); openSwitcher(drop); };
  const scheduleClose = () => { cancelClose(); closeTimer = setTimeout(() => closeSwitcher(drop), 420); };

  drop.addEventListener('mouseenter', open);
  drop.addEventListener('mouseleave', scheduleClose);
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    drop.classList.contains('open') ? closeSwitcher(drop) : open();
  });
  drop.addEventListener('focusin', cancelClose);
  drop.addEventListener('focusout', (e) => {
    if (!drop.contains(e.relatedTarget)) scheduleClose();
  });
  return drop;
}

function openSwitcher(drop) {
  drop.classList.add('open');
  const t = drop.querySelector('.switch-trigger');
  if (t) t.setAttribute('aria-expanded', 'true');
}
function closeSwitcher(drop) {
  drop.classList.remove('open');
  const t = drop.querySelector('.switch-trigger');
  if (t) t.setAttribute('aria-expanded', 'false');
}

function switchTitle(state, on) {
  if (on) return `Currently ${state}`;
  if (state === 'dormant') return 'End the current phase (rest)';
  const cur = HOME ? HOME.phase_state : 'dormant';
  if (cur === 'dormant') return `Start a new ${state} phase`;
  return `Switch to ${state}`;
}

/* Tapping a state. Same-state = gentle no-op. Otherwise confirm, then act. */
function onSwitchTap(target) {
  const current = HOME ? HOME.phase_state : 'dormant';
  if (target === current) {
    toast(`You’re already ${current === 'dormant' ? 'resting' : 'in a ' + current + ' phase'}.`);
    return;
  }

  // dormant -> active/casual : start a new phase (needs a name).
  if (current === 'dormant') {
    openStartPhaseModal(target);
    return;
  }

  // in a phase, tap dormant : end the phase (rest).
  if (target === 'dormant') {
    confirmSwitch(
      'End this phase?',
      'Ending a phase is safe: nothing is deleted, and all your snapshots and history stay browsable. The app simply rests until you start a new phase. Comparison only ever happens within a phase, so this just closes the current chapter.',
      'End phase', 'danger',
      async () => { await api.post('/api/phase/end', {}); toast('Phase ended. Resting now.'); }
    );
    return;
  }

  // in a phase, tap the other of active/casual : ask whether this is a
  // continuation of the SAME phase (keep history, optionally rename) or a NEW
  // phase entirely (end this one, start fresh). Post-Phase-O.
  openSwitchTypeModal(target);
}

/* The switch-type decision (Post-Phase-O). Moving active<->casual is no longer a
   silent warm switch — it asks what the user means:
     • "Same phase"  → warm switch_type (keeps comparison history); offers an
                       optional rename, prefilled with the current name. Trends
                       still show the active/casual split because each trend row
                       is stamped with the phase's type at the time it's written.
     • "New phase"   → end the current phase and start a fresh one of the new
                       type (fresh baseline, old phase archived & browsable).
   Two-step: choose, then (for "new") name it. */
function openSwitchTypeModal(target) {
  const curName = (HOME && HOME.phase && HOME.phase.name) ? HOME.phase.name : 'this phase';
  const targetLabel = target === 'active' ? 'active hunt' : 'casual watch';

  // Optional rename for the "same phase" path — prefilled, editable.
  const renameInput = el('input', {
    type: 'text', id: 'phaseRename', value: (HOME && HOME.phase && HOME.phase.name) ? HOME.phase.name : '',
    placeholder: 'Keep or change the name',
  });

  const sameBtn = el('button', { class: 'btn signal' }, 'Keep this phase');
  sameBtn.addEventListener('click', async () => {
    sameBtn.disabled = true;
    try {
      const newName = renameInput.value.trim();
      await api.post('/api/phase/switch', { type: target, name: newName || undefined });
      toast(newName && newName !== curName
        ? `Switched to ${targetLabel}, renamed to “${newName}”.`
        : `Switched to ${targetLabel}. Same phase, history kept.`);
      await refreshAfterPhaseChange();
      veil.remove();
    } catch (e) { toast(e.message, true); sameBtn.disabled = false; }
  });

  const newBtn = el('button', { class: 'btn ghost' }, 'Start a new phase');
  newBtn.addEventListener('click', () => {
    veil.remove();
    // Reuse the existing start-phase naming modal, but first end the current
    // phase so the new one begins a clean baseline. We chain them: end → name → create.
    openStartPhaseModal(target, { endCurrentFirst: true });
  });

  const veil = modal(
    `Moving to ${targetLabel}`,
    `Is this still the same chapter of your search, or a fresh start? Keeping the ` +
    `phase preserves your comparison history and shows the active/casual change ` +
    `on your trends. Starting a new phase archives “${curName}” and begins a clean ` +
    `baseline.`,
    [
      el('div', { class: 'field' }, [
        el('label', { for: 'phaseRename' }, 'Name (if keeping this phase)'),
        renameInput,
      ]),
      el('div', { class: 'switch-choice-actions' }, [ sameBtn, newBtn ]),
    ],
    null,   // no default confirm button — the two choice buttons drive it
    null);
  // The generic modal() adds a Cancel + Confirm row; hide its Confirm since our
  // two buttons are the real choices. (Cancel stays as the backout.)
  const defaultConfirm = veil.querySelector('.modal-actions .btn.signal');
  if (defaultConfirm) defaultConfirm.remove();
  document.body.appendChild(veil);
  renameInput.focus();
  renameInput.select();
}

/* Shared post-phase-change refresh: pull fresh home state, repaint theme, redraw
   banner + current route. Used by every phase mutation so the switcher label,
   colours, and reading all reflect the change at once. */
async function refreshAfterPhaseChange() {
  HOME = await api.get('/api/home');
  applyTheme();
  renderBanner();
  ROUTES[CURRENT_ROUTE]();
}

/* A small confirm dialog wrapper around the existing modal(). Runs the action,
   then refreshes Home state and re-renders the current route so the switcher
   and reading reflect the change immediately (re-theming is Phase H). */
function confirmSwitch(title, body, label, kind, action) {
  const veil = modal(title, body, [], async () => {
    try {
      await action();
      HOME = await api.get('/api/home');
      applyTheme();        // (H) the switch repaints the whole app
      renderBanner();
      ROUTES[CURRENT_ROUTE]();
      return true;
    } catch (e) { toast(e.message, true); return false; }
  }, label);
  if (kind === 'danger') {
    const btn = veil.querySelector('.btn.signal');
    if (btn) { btn.classList.remove('signal'); btn.classList.add('danger'); }
  }
  document.body.appendChild(veil);
}

/* Start a new phase of a chosen type. From dormant it just creates; from the
   switch-type "new phase" branch, opts.endCurrentFirst ends the live phase first
   so the new one begins a clean baseline (the old phase is archived, browsable).
   create_phase already ends any open phase on the same day, so we rely on that
   single-open-phase guarantee rather than a separate end call — passing the flag
   only changes the wording so the user knows the current phase is being closed. */
function openStartPhaseModal(type, opts = {}) {
  const endingFirst = !!opts.endCurrentFirst;
  const nameInput = el('input', { type: 'text', placeholder: type === 'active' ? 'e.g. Spring 2026 hunt' : 'e.g. Casual market watch', id: 'phaseName' });
  const baseBody = type === 'active'
    ? 'An active phase is a real job hunt — you’ll check every few days and see what’s new first. Starting one begins a fresh baseline: your first check records everything as the starting point, with no “removed” noise.'
    : 'A casual phase is a market watch — a looser rhythm, no nagging, with trends to the fore. Starting one begins a fresh baseline; your first check records the starting point.';
  const body = endingFirst
    ? baseBody + ' Your current phase will be closed and kept in History.'
    : baseBody;
  const veil = modal(
    type === 'active' ? 'Start an active hunt' : 'Start a casual watch',
    body,
    [ el('div', { class: 'field' }, [ el('label', { for: 'phaseName' }, 'Name this phase'), nameInput ]) ],
    async () => {
      const name = nameInput.value.trim();
      if (!name) { toast('Give the phase a name first.', true); return false; }
      try {
        await api.post('/api/phase/create', { name, type });
        toast(`${type === 'active' ? 'Active hunt' : 'Casual watch'} started.`);
        await refreshAfterPhaseChange();
        return true;
      } catch (e) { toast(e.message, true); return false; }
    },
    type === 'active' ? 'Start hunt' : 'Start watch');
  document.body.appendChild(veil);
  nameInput.focus();
}

/* ---- small inline SVG glyphs (no icon library) ------------------------ */

function svgEl(viewBox, paths, cls) {
  const SVGNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', viewBox);
  svg.setAttribute('class', cls || 'glyph');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const p of paths) {
    const node = document.createElementNS(SVGNS, p.t || 'path');
    for (const [k, v] of Object.entries(p)) { if (k !== 't') node.setAttribute(k, v); }
    svg.appendChild(node);
  }
  return svg;
}

/* Each state gets a glyph that reads as what it is:
   active = a filled pulse dot with a ring (live, attentive);
   casual = a gentle wave (drifting, watching);
   dormant = a crescent moon (at rest). */
function stateGlyph(state) {
  if (state === 'active') {
    return svgEl('0 0 24 24', [
      { t: 'circle', cx: 12, cy: 12, r: 9, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6, opacity: .5 },
      { t: 'circle', cx: 12, cy: 12, r: 4, fill: 'currentColor' },
    ], 'glyph');
  }
  if (state === 'casual') {
    return svgEl('0 0 24 24', [
      { t: 'path', d: 'M3 13c2.5-3 4.5-3 7 0s4.5 3 7 0', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round' },
      { t: 'path', d: 'M3 17c2.5-3 4.5-3 7 0s4.5 3 7 0', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', opacity: .45 },
    ], 'glyph');
  }
  // dormant — crescent moon
  return svgEl('0 0 24 24', [
    { t: 'path', d: 'M20 14.5A8 8 0 1 1 10.5 4a6.5 6.5 0 0 0 9.5 10.5z', fill: 'currentColor' },
  ], 'glyph');
}

/* A proper settings cog: a toothed gear ring with a hollow centre. */
function cogGlyph() {
  return svgEl('0 0 24 24', [
    { t: 'path',
      d: 'M10.39 4.78 L10.21 1.75 L13.79 1.75 L13.61 4.78 L15.97 5.75 L17.98 3.49 L20.51 6.02 L18.25 8.03 L19.22 10.39 L22.25 10.21 L22.25 13.79 L19.22 13.61 L18.25 15.97 L20.51 17.98 L17.98 20.51 L15.97 18.25 L13.61 19.22 L13.79 22.25 L10.21 22.25 L10.39 19.22 L8.03 18.25 L6.02 20.51 L3.49 17.98 L5.75 15.97 L4.78 13.61 L1.75 13.79 L1.75 10.21 L4.78 10.39 L5.75 8.03 L3.49 6.02 L6.02 3.49 L8.03 5.75 Z',
      fill: 'none', stroke: 'currentColor', 'stroke-width': 1.4, 'stroke-linejoin': 'round' },
    { t: 'circle', cx: 12, cy: 12, r: 3.1, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.4 },
  ], 'glyph');
}

/* A small downward caret for the phase dropdown trigger. */
function caretGlyph() {
  return svgEl('0 0 24 24', [
    { t: 'path', d: 'M6 9l6 6 6-6', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
  ], 'caret-glyph');
}

/* A trash-can for deleting a phase from History — lid, body, and two staves. */
function trashGlyph() {
  return svgEl('0 0 24 24', [
    { t: 'path', d: 'M4 7h16', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round' },
    { t: 'path', d: 'M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
    { t: 'path', d: 'M6 7l1 12.5A1.5 1.5 0 0 0 8.5 21h7a1.5 1.5 0 0 0 1.5-1.5L18 7', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
    { t: 'path', d: 'M10 11v6M14 11v6', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round' },
  ], 'glyph');
}

/* Appearance glyphs (H): sun = light, moon = dark, half-disc = system/auto. */
function appearanceGlyph(mode) {
  if (mode === 'light') {
    return svgEl('0 0 24 24', [
      { t: 'circle', cx: 12, cy: 12, r: 4.2, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6 },
      { t: 'path', d: 'M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5.2 5.2l1.9 1.9M16.9 16.9l1.9 1.9M18.8 5.2l-1.9 1.9M7.1 16.9l-1.9 1.9',
        fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6, 'stroke-linecap': 'round' },
    ], 'glyph');
  }
  if (mode === 'dark') {
    return svgEl('0 0 24 24', [
      { t: 'path', d: 'M20 14.5A8 8 0 1 1 10.5 4a6.5 6.5 0 0 0 9.5 10.5z', fill: 'currentColor' },
    ], 'glyph');
  }
  // system / auto — a disc split light|dark
  return svgEl('0 0 24 24', [
    { t: 'circle', cx: 12, cy: 12, r: 8.4, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6 },
    { t: 'path', d: 'M12 3.6a8.4 8.4 0 0 0 0 16.8z', fill: 'currentColor' },
  ], 'glyph');
}

/* =======================================================================
   HOME (G.3) — the conditional landing screen. NOT the old renderHomeMenu.
   No banner by default (hover reveals it). A settings icon top-right is always
   present. Content branches on the remembered phase type (read from /api/home,
   which the engine persists in phases.json).
   ======================================================================= */

async function renderHome() {
  // Home is purpose-built: the hero IS the phase statement, so we clear the
  // reading spine here (it stays as context above the other routes' screens).
  $('#reading').replaceChildren();
  renderHomeContent();
}

function renderHomeContent() {
  const view = $('#view');
  const phase = HOME.phase;
  const ps = HOME.phase_state; // dormant | active | casual
  const companyCount = HOME.company_count || 0;

  // No topbar/masthead — the phase switcher and settings cog live in the banner
  // (revealed by hovering/focusing the top edge on Home). The content is centred
  // in the viewport (Claude.ai-style), built inside .home-wrap.
  const inner = [];
  if (ps === 'dormant') {
    inner.push(...homeDormant(companyCount));
  } else if (ps === 'casual') {
    inner.push(...homeOpen('casual', phase, companyCount));
  } else {
    inner.push(...homeOpen('active', phase, companyCount));
  }

  view.replaceChildren(el('div', { class: 'home-wrap' }, inner));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* Dormant Home — "Want to review jobs and roles?" → Yes routes to Companies &
   Buckets (the review page; Phase I builds its detail). No-pressure register. */
function homeDormant(companyCount) {
  const card = el('div', { class: 'home-hero is-dormant' }, [
    el('p', { class: 'home-eyebrow' }, 'Resting'),
    el('h1', { class: 'home-q' }, 'Want to review jobs and roles?'),
    el('p', { class: 'home-sub' }, companyCount
      ? 'Your history is safe and waiting. Pick up where you left off — look over your saved companies and buckets, and start a phase whenever you’re ready to hunt again.'
      : 'Nothing’s tracked yet. Add a few companies to watch, then start a phase when you’re ready to hunt.'),
    el('div', { class: 'home-actions' }, [
      el('button', { class: 'btn signal', onclick: () => go('companies') },
        companyCount ? 'Yes — review companies & buckets' : 'Add your first companies'),
      el('button', { class: 'btn ghost', onclick: () => go('trends') }, 'See past trends'),
    ]),
  ]);
  return [card, dormantFootnote()];
}

function dormantFootnote() {
  return el('p', { class: 'home-foot' }, [
    'To begin a new hunt, open the menu at the top and use the phase dropdown to pick ',
    el('b', {}, 'Active'),
    ' or ',
    el('b', {}, 'Casual'),
    ' — that starts a fresh phase without touching anything you’ve recorded.',
  ]);
}

/* Casual / Active Home — show when roles were last reviewed and the options.
   Casual: resume-&-check + adjust buckets + tracker. Active: those + trends. */
function homeOpen(type, phase, companyCount) {
  const isActive = type === 'active';
  const last = phase && phase.last_check;
  const buckets = HOME.buckets || [];
  const runnable = buckets.filter(b => b.runnable_count > 0);

  const lastLine = last
    ? `Last reviewed ${last}.`
    : 'Not checked yet — your first check sets the baseline.';

  const hero = el('div', { class: 'home-hero is-' + type }, [
    el('p', { class: 'home-eyebrow' }, isActive ? 'Job hunt' : 'Market watch'),
    el('h1', { class: 'home-q' }, phase ? phase.name : (isActive ? 'Active hunt' : 'Casual watch')),
    el('p', { class: 'home-sub' }, lastLine),
  ]);

  const actions = el('div', { class: 'home-actions' });

  // (1) Resume & check — kicks off a run. If there's exactly one runnable
  // bucket, go straight in; otherwise let the user pick on the review page.
  if (runnable.length === 1) {
    actions.appendChild(el('button', { class: 'btn signal', onclick: () => startRun(runnable[0].name) },
      isActive ? `Resume & check “${runnable[0].name}”` : `Look in on “${runnable[0].name}”`));
  } else if (runnable.length > 1) {
    actions.appendChild(el('button', { class: 'btn signal', onclick: () => go('home') === null ? null : openBucketPicker(isActive) },
      isActive ? 'Resume & check changes' : 'Look in & check changes'));
  } else {
    // No runnable bucket (G.7 cold state) — guide toward companies.
    actions.appendChild(el('button', { class: 'btn signal', onclick: () => go('companies') },
      companyCount ? 'Set up a bucket to check' : 'Add companies to check'));
  }

  // (2) Adjust buckets/companies.
  actions.appendChild(el('button', { class: 'btn ghost', onclick: () => go('companies') }, 'Adjust buckets & companies'));

  // Tracker button (G.8) — casual and active both get it.
  actions.appendChild(el('button', { class: 'btn ghost', onclick: () => go('tracker') }, 'Application tracker'));

  // (3) Active only: review trends.
  if (isActive) {
    actions.appendChild(el('button', { class: 'btn ghost', onclick: () => go('trends') }, 'Review trends'));
  }

  hero.appendChild(actions);

  const extras = [];
  // A quiet link back to the last report, if one exists this session or on disk.
  extras.push(lastReportLink());

  return [hero, ...extras];
}

/* When more than one bucket is runnable, the "resume & check" button opens a
   small picker rather than guessing which bucket. */
function openBucketPicker(isActive) {
  const buckets = (HOME.buckets || []).filter(b => b.runnable_count > 0);
  const veil = el('div', { class: 'modal-veil' });
  veil.addEventListener('click', (e) => { if (e.target === veil) veil.remove(); });
  const list = el('div', { class: 'bucket-grid', style: 'grid-template-columns:1fr' },
    buckets.map(b => el('button', { class: 'bucket', onclick: () => { veil.remove(); startRun(b.name); } }, [
      el('div', { class: 'b-name' }, b.name),
      el('div', { class: 'b-meta' }, b.runnable_count === b.company_count
        ? `${b.company_count} ${plural(b.company_count, 'company', 'companies')}`
        : `${b.runnable_count} of ${b.company_count} ready`),
      el('div', { class: 'b-go' }, isActive ? 'Check now →' : 'Look in →'),
    ])));
  veil.appendChild(el('div', { class: 'modal' }, [
    el('h2', {}, isActive ? 'Which bucket?' : 'Look in on which bucket?'),
    el('p', {}, 'Pick a group to check now.'),
    list,
    el('div', { class: 'modal-actions' }, [ el('button', { class: 'btn ghost', onclick: () => veil.remove() }, 'Cancel') ]),
  ]));
  document.body.appendChild(veil);
}

/* A small, quiet "Latest results" link shown on open-phase Home if a saved
   report exists (G.9). Checks /api/last-report so it survives restarts. */
function lastReportLink() {
  const wrap = el('div', { class: 'home-lastreport', hidden: '' });
  api.get('/api/last-report').then(r => {
    if (r && r.result) {
      wrap.hidden = false;
      const when = r.saved_at ? r.saved_at.slice(0, 10) : null;
      wrap.replaceChildren(el('button', { class: 'backlink', onclick: () => go('report') }, [
        '↩ Latest results', when ? el('span', { class: 'muted', style: 'margin-left:8px;font-size:12px' }, `from ${when}`) : null,
      ]));
    }
  }).catch(() => {});
  return wrap;
}

function renderLoading() {
  $('#reading').replaceChildren();
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Reading your job search…'));
}

function renderReading() {
  const region = $('#reading');
  const ps = HOME.phase_state; // dormant | active | casual
  const phase = HOME.phase;

  if (!phase) {
    region.replaceChildren(el('div', { class: 'reading is-dormant' }, [
      el('p', { class: 'eyebrow' }, 'Resting'),
      el('p', { class: 'phase-name' }, 'No active phase right now'),
      el('p', { class: 'phase-note' },
        'Your history is safe and waiting. When you’re ready to hunt again, start a phase — that begins a clean baseline without touching anything you’ve recorded.'),
      el('div', { class: 'btn-row', style: 'margin-top:16px' }, [
        el('button', { class: 'btn ghost small', onclick: openArchive }, 'Browse past phases'),
        el('button', { class: 'btn ghost small', onclick: () => go('trends') }, 'See trends'),
      ]),
    ]));
    return;
  }

  // Active or casual: a clean, width-using banner. We deliberately drop the
  // cadence "rhythm" line here — on the Jobs page the useful context is simply
  // which hunt this is and when it was last checked, not a nag about timing.
  const stateClass = ps === 'casual' ? 'is-casual' : 'is-active';

  const meta = el('div', { class: 'phase-meta' }, [
    el('span', {}, ['since ', el('b', {}, phase.started_on || '–')]),
    el('span', {}, phase.last_check
      ? ['last checked ', el('b', {}, phase.last_check)]
      : [el('b', {}, 'never checked'), ' yet']),
  ]);

  region.replaceChildren(el('div', { class: 'reading ' + stateClass }, [
    el('div', { class: 'reading-main' }, [
      el('p', { class: 'eyebrow' }, ps === 'casual' ? 'Market watch' : 'Job hunt'),
      el('p', { class: 'phase-name' }, phase.name),
    ]),
    meta,
  ]));
}

/* =======================================================================
   STUB ROUTES (G.5) — the navigation exists now; later phases fill them.
   Application Tracker = Phase L (still a stub). (Set-up became the Guide in
   Phase J — see renderGuide.) The stub is a real, reachable route with an
   honest "coming" page, so adding the real screen later doesn't reshuffle the
   shell.
   ======================================================================= */

/* =======================================================================
   APPLICATION TRACKER (Phase L) — the durable record of roles you've applied
   to, as a 7-column table you manage, tied to the current phase. Unlike Saved
   jobs (which resets on dormancy), the tracker SURVIVES dormancy.

   Columns: Job Title · Company · Date Applied · Status · Screening interview ·
   Rounds of interview · Notes.

   Statuses (one-way live ladder + terminal): applied → screening → interview,
   then any of ghosted / offer / rejected / withdrawn. The server enforces the
   ladder; here we only OFFER legal moves so it never reads as broken.

   Auto-ghost (handled server-side): a live row untouched for 14 days reads as
   ghosted on load. Filters: Status · Company · Date applied · Screening.
   ======================================================================= */

/* in-memory state for the tracker (no browser storage) */
let TRACKER = null;                 // last /api/applications payload
// Filters: Status + Company are SETS (multi-select checkboxes — empty = all).
// Screening is a tri-state checkbox group (which of yes/no to include — empty/
// both = all). Sorting replaces the old "date filter": sort by any column, asc
// or desc.
let TRACK_FILTERS = { statuses: new Set(), companies: new Set(), screening: new Set() };
let TRACK_SORT = { key: 'applied_on', dir: 'desc' };  // key: applied_on|company_name|title|status

/* The statuses, grouped, with their fixed semantic colours (L.2). These are
   meaning, not theme decoration, so they stay constant across all H themes —
   the colour classes live in app.css (.st-applied etc.). */
const STATUS_GROUPS = [
  ['To-do',       [['applied', 'Applied']]],
  ['In progress', [['screening', 'Screening'], ['interview', 'Interview']]],
  ['Complete',    [['ghosted', 'Ghosted'], ['offer', 'Offer'],
                   ['rejected_before_interview', 'Rejected (before interview)'],
                   ['rejected_after_interview', 'Rejected (after interview)'],
                   ['withdrawn', 'Withdrawn']]],
];
const STATUS_LABEL = {
  applied: 'Applied', screening: 'Screening', interview: 'Interview',
  ghosted: 'Ghosted', offer: 'Offer', withdrawn: 'Withdrawn',
  rejected_before_interview: 'Rejected (before interview)',
  rejected_after_interview: 'Rejected (after interview)',
  /* LEGACY: rows written before rejections were staged. Still rendered so old
     applications display and can be corrected; never OFFERED as a new choice
     (it's absent from TERMINAL_STATUSES below, which drives the picker). */
  rejected: 'Rejected (stage not recorded)',
};
const LIVE_LADDER = ['applied', 'screening', 'interview'];
/* Selectable terminals — legacy 'rejected' is deliberately excluded so the
   picker only offers staged rejections from now on. A row sitting on the legacy
   value can still be corrected TO one of these (legalNextStatuses treats any
   unknown-to-the-ladder status as terminal). */
const TERMINAL_STATUSES = ['ghosted', 'offer', 'rejected_before_interview',
                           'rejected_after_interview', 'withdrawn'];

/* The legal moves FROM a given status, mirroring applications.py's rule, so the
   picker only shows what the server will accept. Forward ladder steps + all
   terminals from a live status; terminal→terminal corrections; nothing back to
   live from a terminal. */
function legalNextStatuses(current) {
  const out = [];
  const li = LIVE_LADDER.indexOf(current);
  if (li !== -1) {
    // forward ladder steps
    for (let i = li + 1; i < LIVE_LADDER.length; i++) out.push(LIVE_LADDER[i]);
    // any terminal
    out.push(...TERMINAL_STATUSES);
  } else {
    // current is terminal — only corrections to another terminal
    out.push(...TERMINAL_STATUSES.filter(s => s !== current));
  }
  return out;
}

async function renderTracker() {
  $('#view').replaceChildren(renderLoading());
  try {
    TRACKER = await api.get('/api/applications');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t load your application tracker.', e.message));
    return;
  }
  drawTracker(true);
}

function drawTracker(scrollTop = false) {
  const view = $('#view');
  const apps = (TRACKER && TRACKER.applications) || [];
  const phaseName = TRACKER && TRACKER.phase ? TRACKER.phase.name : null;

  const head = el('div', { class: 'sec-head' }, [
    el('h2', {}, 'Application tracker'),
    el('button', { class: 'btn small', onclick: openManualAdd }, '+ Add a role'),
  ]);

  // No phase at all → nothing to track yet.
  if (TRACKER && !TRACKER.phase && !apps.length) {
    view.replaceChildren(head, el('div', { class: 'empty' }, [
      el('h3', {}, 'No active phase'),
      el('p', {}, 'Applications are tied to a job-hunt phase. Start a phase, then the roles you apply to will be recorded here — automatically when you press “Applied” on a saved role, or by adding one yourself.'),
    ]));
    if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  if (!apps.length) {
    view.replaceChildren(head,
      phaseName ? el('p', { class: 'lead' }, `Tracking applications for ${phaseName}.`) : null,
      el('div', { class: 'empty' }, [
        el('h3', {}, 'No applications yet'),
        el('p', {}, 'Press “Applied” on a saved role in the Jobs tab and it lands here, or add a role you saw elsewhere with “Add a role”.'),
      ]));
    if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  const filtered = applyTrackerFilters(apps);
  const wrap = el('div', { class: 'tracker-wrap' }, [
    phaseName ? el('p', { class: 'lead' }, `Tracking applications for ${phaseName}.`) : null,
    trackerFilterBar(apps),
    filtered.length
      ? trackerTable(filtered)
      : el('div', { class: 'empty' }, [ el('p', {}, 'No applications match these filters.') ]),
  ]);
  view.replaceChildren(head, wrap);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---- filters + sort (L.8, reworked) -----------------------------------
   Status & Company: multi-select (tick several; empty set = all). Screening:
   tick Yes and/or No (empty or both = all). Sort: any column, asc/desc. */

/* Sort order for the tracker's status column. Includes the legacy 'rejected'
   so old rows sort sensibly rather than falling off the end. */
const STATUS_ORDER = ['applied', 'screening', 'interview', 'ghosted', 'offer',
                      'rejected_before_interview', 'rejected_after_interview',
                      'rejected', 'withdrawn'];

function applyTrackerFilters(apps) {
  const f = TRACK_FILTERS;
  let out = apps.filter(a => {
    if (f.statuses.size && !f.statuses.has(a.status)) return false;
    if (f.companies.size && !f.companies.has(a.company_name)) return false;
    if (f.screening.size) {
      const key = a.screening_interview ? 'yes' : 'no';
      if (!f.screening.has(key)) return false;
    }
    return true;
  });
  return sortTrackerRows(out);
}

function sortTrackerRows(rows) {
  const { key, dir } = TRACK_SORT;
  const sign = dir === 'asc' ? 1 : -1;
  const val = (a) => {
    if (key === 'status') return String(STATUS_ORDER.indexOf(a.status)).padStart(2, '0');
    if (key === 'company_name') return (a.company_name || '').toLowerCase();
    if (key === 'title') return (a.title || '').toLowerCase();
    return a.applied_on || '';   // applied_on (ISO sorts lexically)
  };
  return rows.slice().sort((x, y) => {
    const vx = val(x), vy = val(y);
    if (vx < vy) return -1 * sign;
    if (vx > vy) return 1 * sign;
    // stable tiebreak by applied date then title
    return (y.applied_on || '').localeCompare(x.applied_on || '')
        || (x.title || '').localeCompare(y.title || '');
  });
}

function anyFilterActive() {
  const f = TRACK_FILTERS;
  return f.statuses.size || f.companies.size || f.screening.size;
}

function trackerFilterBar(apps) {
  const usedStatuses = STATUS_ORDER.filter(s => apps.some(a => a.status === s));
  const companies = (TRACKER.company_names || []).filter(Boolean);

  const statusDrop = checkboxDropdown({
    label: 'Status', allLabel: 'All statuses',
    options: usedStatuses.map(s => ({ value: s, label: STATUS_LABEL[s], dot: 'st-' + s })),
    selected: TRACK_FILTERS.statuses,
    onToggle: (v, on) => { on ? TRACK_FILTERS.statuses.add(v) : TRACK_FILTERS.statuses.delete(v); drawTracker(); },
  });

  const companyDrop = checkboxDropdown({
    label: 'Company', allLabel: 'All companies',
    options: companies.map(c => ({ value: c, label: c })),
    selected: TRACK_FILTERS.companies,
    onToggle: (v, on) => { on ? TRACK_FILTERS.companies.add(v) : TRACK_FILTERS.companies.delete(v); drawTracker(); },
  });

  const screenDrop = checkboxDropdown({
    label: 'Screening', allLabel: 'Screening: any',
    options: [{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }],
    selected: TRACK_FILTERS.screening,
    onToggle: (v, on) => { on ? TRACK_FILTERS.screening.add(v) : TRACK_FILTERS.screening.delete(v); drawTracker(); },
  });

  // Sort control: a column picker + an asc/desc toggle.
  const sortSel = el('select', {
    class: 'tf-select',
    onchange: (e) => { TRACK_SORT.key = e.target.value; drawTracker(); },
  }, [
    ['applied_on', 'Date applied'], ['company_name', 'Company'],
    ['title', 'Job title'], ['status', 'Status'],
  ].map(([v, l]) => el('option', { value: v, selected: TRACK_SORT.key === v ? '' : null }, l)));

  const dirBtn = el('button', {
    class: 'tf-dir', title: TRACK_SORT.dir === 'asc' ? 'Ascending' : 'Descending',
    onclick: () => { TRACK_SORT.dir = TRACK_SORT.dir === 'asc' ? 'desc' : 'asc'; drawTracker(); },
  }, TRACK_SORT.dir === 'asc' ? '↑' : '↓');

  const clearBtn = anyFilterActive()
    ? el('button', { class: 'btn ghost small', onclick: () => {
        TRACK_FILTERS = { statuses: new Set(), companies: new Set(), screening: new Set() };
        drawTracker();
      } }, 'Clear filters')
    : null;

  return el('div', { class: 'tracker-controls' }, [
    el('div', { class: 'tracker-filters' }, [
      el('span', { class: 'tf-label' }, 'Filter'),
      statusDrop, companyDrop, screenDrop, clearBtn,
    ]),
    el('div', { class: 'tracker-sort' }, [
      el('span', { class: 'tf-label' }, 'Sort'),
      sortSel, dirBtn,
    ]),
  ]);
}

/* A reusable checkbox dropdown: a button showing the current selection summary,
   opening a panel of tickable options. Empty selection reads as "all". */
function checkboxDropdown({ label, allLabel, options, selected, onToggle }) {
  const summary = selected.size === 0
    ? allLabel
    : (selected.size === 1
        ? (options.find(o => selected.has(o.value)) || {}).label || `${label} (1)`
        : `${label} (${selected.size})`);

  const btn = el('button', {
    class: 'tf-drop' + (selected.size ? ' on' : ''),
    onclick: (e) => { e.stopPropagation(); openCheckboxPanel(btn, options, selected, onToggle); },
  }, [ summary, el('span', { class: 'tf-caret' }, '▾') ]);
  return btn;
}

function openCheckboxPanel(anchor, options, selected, onToggle) {
  closeAnyMenu();
  const panel = el('div', { class: 'cb-panel' },
    options.map(o => {
      const box = el('input', { type: 'checkbox' });
      box.checked = selected.has(o.value);
      box.addEventListener('change', () => onToggle(o.value, box.checked));
      return el('label', { class: 'cb-item' }, [
        box,
        o.dot ? el('span', { class: 'st-dot ' + o.dot }) : null,
        el('span', {}, o.label),
      ]);
    }));
  document.body.appendChild(panel);
  const r = anchor.getBoundingClientRect();
  panel.style.top = (window.scrollY + r.bottom + 4) + 'px';
  panel.style.left = (window.scrollX + r.left) + 'px';
  installOutsideClose();
}

/* Close any open popover when the user points down OUTSIDE both the popover and
   a popover trigger. Crucially we do NOT use a full-screen veil — a veil would
   swallow the first click on another filter button, forcing a second click to
   open it. With this listener, clicking a different trigger closes the current
   popover (via that trigger's own openX → closeAnyMenu) AND opens the new one in
   the same click. Triggers carry .tf-drop or .status-pill; popovers are
   .cb-panel / .status-menu. */
let _outsideCloseInstalled = false;
function installOutsideClose() {
  if (_outsideCloseInstalled) return;
  _outsideCloseInstalled = true;
  document.addEventListener('pointerdown', _maybeCloseMenus, true);
}
function _maybeCloseMenus(e) {
  // Nothing open → uninstall and bail.
  if (!document.querySelector('.cb-panel, .status-menu')) {
    document.removeEventListener('pointerdown', _maybeCloseMenus, true);
    _outsideCloseInstalled = false;
    return;
  }
  const t = e.target;
  // Click inside an open popover: leave it (checkbox ticking, menu items).
  if (t.closest && t.closest('.cb-panel, .status-menu')) return;
  // Click on a trigger: that trigger's own handler will close+reopen, so don't
  // pre-close here (pre-closing then reopening is fine too, but this avoids a
  // flfrom-flash). Either way, the single click lands on the button.
  if (t.closest && t.closest('.tf-drop, .status-pill')) return;
  // Genuine outside click → close.
  closeAnyMenu();
}

/* ---- the table ------------------------------------------------------- */

function trackerTable(rows) {
  const headerCells = ['Job title', 'Company', 'Date applied', 'Status',
                       'Screening', 'Rounds', 'Notes', ''];
  const thead = el('div', { class: 'tr-row tr-head' },
    headerCells.map(h => el('div', { class: 'tr-cell' }, h)));
  const body = rows.map(trackerRow);
  return el('div', { class: 'tracker-table', role: 'table' }, [thead, ...body]);
}

function trackerRow(a) {
  // Title (links out) + the "no longer listed" marker (L.5) + flag tags (M.5).
  const titleNode = a.url
    ? el('a', { href: a.url, target: '_blank', rel: 'noopener' }, a.title || '(untitled role)')
    : document.createTextNode(a.title || '(untitled role)');
  const tags = flagTags(a);  // M.5 — re-flagged on read by the server
  const titleCell = el('div', { class: 'tr-cell tr-title' }, [
    el('div', { class: 'tr-title-text' }, [titleNode]),
    a.no_longer_listed
      ? el('span', { class: 'tr-gone', title: 'This role is no longer in the company’s latest check.' }, 'no longer listed')
      : null,
    tags.length ? el('div', { class: 'tr-tags' }, tags) : null,
  ]);

  return el('div', { class: 'tr-row' }, [
    titleCell,
    el('div', { class: 'tr-cell' }, a.company_name || ''),
    el('div', { class: 'tr-cell tr-date' }, fmtLongDate(a.applied_on) || '—'),
    el('div', { class: 'tr-cell' }, [ statusControl(a) ]),
    el('div', { class: 'tr-cell' }, [ screeningToggle(a) ]),
    el('div', { class: 'tr-cell' }, [ roundsControl(a) ]),
    el('div', { class: 'tr-cell tr-notes' }, [ notesControl(a) ]),
    el('div', { class: 'tr-cell tr-actions' }, [
      el('button', {
        class: 'btn ghost small danger', title: 'Remove this application',
        onclick: () => removeApplication(a),
      }, 'Remove'),
    ]),
  ]);
}

/* Status: a coloured pill that opens a small menu of LEGAL next statuses. */
function statusControl(a) {
  const pill = el('button', {
    class: 'status-pill st-' + a.status,
    onclick: (e) => { e.stopPropagation(); openStatusMenu(a, pill); },
    title: 'Change status',
  }, [ el('span', { class: 'st-dot' }), STATUS_LABEL[a.status] || a.status,
       el('span', { class: 'st-caret' }, '▾') ]);
  return pill;
}

function openStatusMenu(a, anchor) {
  closeAnyMenu();
  const next = legalNextStatuses(a.status);
  if (!next.length) { toast('This application is already at a final outcome.'); return; }

  const menu = el('div', { class: 'status-menu' });
  STATUS_GROUPS.forEach(([groupName, items]) => {
    const avail = items.filter(([s]) => next.includes(s));
    if (!avail.length) return;
    menu.appendChild(el('div', { class: 'sm-group' }, groupName));
    avail.forEach(([s, label]) => {
      menu.appendChild(el('button', {
        class: 'sm-item st-' + s,
        onclick: () => { closeAnyMenu(); changeStatus(a, s); },
      }, [ el('span', { class: 'st-dot' }), label ]));
    });
  });

  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = (window.scrollY + r.bottom + 4) + 'px';
  menu.style.left = (window.scrollX + r.left) + 'px';
  installOutsideClose();
}

function closeAnyMenu() {
  document.querySelectorAll('.status-menu, .cb-panel, .menu-veil').forEach(n => n.remove());
  if (_outsideCloseInstalled) {
    document.removeEventListener('pointerdown', _maybeCloseMenus, true);
    _outsideCloseInstalled = false;
  }
}

async function changeStatus(a, newStatus) {
  try {
    await api.post('/api/applications/update', {
      company_key: a.company_key, id: a.id, phase_id: a.phase_id, status: newStatus,
    });
    await refreshTracker();
  } catch (e) { toast(e.message, true); }
}

/* Screening interview: a Yes/No toggle. No→Yes resets the auto-ghost clock. */
function screeningToggle(a) {
  const yes = !!a.screening_interview;
  return el('button', {
    class: 'yn-toggle' + (yes ? ' on' : ''),
    role: 'switch', 'aria-checked': yes ? 'true' : 'false',
    onclick: () => setScreening(a, !yes),
  }, yes ? 'Yes' : 'No');
}

async function setScreening(a, value) {
  try {
    await api.post('/api/applications/update', {
      company_key: a.company_key, id: a.id, phase_id: a.phase_id,
      screening_interview: value,
    });
    await refreshTracker();
  } catch (e) { toast(e.message, true); }
}

/* Rounds of interview: a small stepper. Never resets the clock. */
function roundsControl(a) {
  const n = a.interview_rounds || 0;
  const dec = el('button', { class: 'rnd-btn', title: 'Fewer',
    onclick: () => setRounds(a, Math.max(0, n - 1)) }, '−');
  const inc = el('button', { class: 'rnd-btn', title: 'More',
    onclick: () => setRounds(a, n + 1) }, '+');
  return el('div', { class: 'rounds' }, [dec, el('span', { class: 'rnd-n' }, String(n)), inc]);
}

async function setRounds(a, rounds) {
  try {
    await api.post('/api/applications/update', {
      company_key: a.company_key, id: a.id, phase_id: a.phase_id,
      interview_rounds: rounds,
    });
    await refreshTracker();
  } catch (e) { toast(e.message, true); }
}

/* Notes: a short preview snippet in the cell + an "Edit" button that opens a
   roomy rich-text editor window (bold, italics, bullet/numbered lists). Notes
   are stored as a restricted HTML subset; we sanitize on the way in AND render
   sanitized. Editing notes never resets the auto-ghost clock. */

/* Strip notes HTML down to a safe subset for display. Parses into a detached
   document and rebuilds, keeping only allowed tags and dropping all attributes
   (so no href/onclick/style/script can ride along). Used for both the cell
   preview and the editor's initial content. */
const _NOTES_ALLOWED = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'UL', 'OL', 'LI', 'P', 'BR']);
function sanitizeNotesHTML(html) {
  const src = document.createElement('div');
  src.innerHTML = html || '';
  const out = document.createElement('div');
  (function walk(from, to) {
    from.childNodes.forEach(node => {
      if (node.nodeType === 3) {            // text
        to.appendChild(document.createTextNode(node.nodeValue));
      } else if (node.nodeType === 1) {     // element
        let tag = node.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE') {
          return;                           // drop element AND its text content
        }
        // Browsers wrap each Enter-separated line in a <div> (sometimes a bare
        // text node + <br>). Treat DIV as a paragraph so the line break SURVIVES
        // sanitizing — previously DIV was unwrapped and every line collapsed
        // onto one. (A <div> that's empty becomes a <br> so blank lines hold.)
        if (tag === 'DIV') {
          if (!node.textContent.trim() && !node.querySelector('img,br,ul,ol')) {
            to.appendChild(document.createElement('br'));
            return;
          }
          tag = 'P';
        }
        if (_NOTES_ALLOWED.has(tag)) {
          const clean = document.createElement(tag);   // no attributes copied
          walk(node, clean);
          to.appendChild(clean);
        } else {
          walk(node, to);                   // unwrap other disallowed tags, keep text
        }
      }
    });
  })(src, out);
  return out.innerHTML;
}

/* A plain-text preview of notes HTML, trimmed for the cell. */
function notesPreview(html) {
  const d = document.createElement('div');
  d.innerHTML = sanitizeNotesHTML(html || '');
  const text = (d.textContent || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text.length > 90 ? text.slice(0, 88) + '…' : text;
}

function notesControl(a) {
  const preview = notesPreview(a.notes);
  return el('div', { class: 'notes-cell' }, [
    preview
      ? el('div', { class: 'notes-preview', title: 'Click Edit to open the full note' }, preview)
      : el('div', { class: 'notes-preview empty-note' }, 'No notes yet'),
    el('button', { class: 'btn ghost small', onclick: () => openNotesEditor(a) },
      preview ? 'Edit' : 'Add'),
  ]);
}

/* The expandable rich-text editor window. A contenteditable surface + a toolbar
   (bold, italic, underline, bullet/numbered lists). Buttons TOGGLE and reflect
   the current selection's state; Cmd/Ctrl+B/I/U work too. Saves sanitized HTML. */
function openNotesEditor(a) {
  const veil = el('div', { class: 'modal-veil', onclick: (e) => { if (e.target === veil) closeEditor(); } });

  const editor = el('div', {
    class: 'notes-editor', contenteditable: 'true', 'aria-label': 'Notes editor',
  });
  editor.innerHTML = sanitizeNotesHTML(a.notes || '');

  // Reflect which commands are active for the current caret/selection. Only
  // sync while the caret is actually inside THIS editor (selectionchange fires
  // document-wide, so guard against syncing when focus is elsewhere).
  const toolButtons = [];
  const syncToolbar = () => {
    const sel = window.getSelection();
    const inEditor = sel && sel.rangeCount > 0
      && editor.contains(sel.getRangeAt(0).commonAncestorContainer);
    toolButtons.forEach(b => {
      let on = false;
      if (inEditor) { try { on = document.queryCommandState(b._cmd); } catch (_) {} }
      b.classList.toggle('on', on);
    });
  };
  // selectionchange is the reliable signal: it fires after the browser updates
  // its internal command state (so the highlight is correct), for BOTH toolbar
  // clicks and keyboard shortcuts and plain caret movement.
  const onSelChange = () => {
    // Self-heal: if the editor was removed (e.g. closed via Escape elsewhere),
    // detach this document-wide listener so it doesn't leak across opens.
    if (!document.body.contains(editor)) {
      document.removeEventListener('selectionchange', onSelChange);
      return;
    }
    syncToolbar();
  };
  document.addEventListener('selectionchange', onSelChange);

  function closeEditor() {
    document.removeEventListener('selectionchange', onSelChange);
    veil.remove();
  }

  const exec = (cmd) => {
    editor.focus();                 // ensure the selection lives in the editor
    document.execCommand(cmd, false, null);
    // execCommand updates state synchronously, but selectionchange may lag a
    // tick in some engines — sync now AND after the microtask, belt-and-braces.
    syncToolbar();
    setTimeout(syncToolbar, 0);
  };

  const makeTool = (cmd, label, title) => {
    const b = el('button', { class: 'ne-tool', title, type: 'button',
      onmousedown: (e) => { e.preventDefault(); exec(cmd); } }, label);  // mousedown keeps selection
    b._cmd = cmd;
    toolButtons.push(b);
    return b;
  };

  const toolbar = el('div', { class: 'notes-toolbar' }, [
    makeTool('bold', el('b', {}, 'B'), 'Bold (⌘/Ctrl+B)'),
    makeTool('italic', el('i', {}, 'I'), 'Italic (⌘/Ctrl+I)'),
    makeTool('underline', el('span', { style: 'text-decoration:underline' }, 'U'), 'Underline (⌘/Ctrl+U)'),
    el('span', { class: 'ne-sep' }),
    makeTool('insertUnorderedList', '• List', 'Bullet list'),
    makeTool('insertOrderedList', '1. List', 'Numbered list'),
  ]);

  // Keyboard shortcuts: Cmd/Ctrl + B/I/U toggle formatting; Cmd/Ctrl + S saves
  // and closes. execCommand toggles; selectionchange refreshes the highlight.
  editor.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey)) return;
    const k = e.key.toLowerCase();
    if (k === 's') {                 // ⌘/Ctrl+S → save (don't trigger browser save)
      e.preventDefault();
      saveAndClose();
      return;
    }
    if (k === 'b' || k === 'i' || k === 'u') {
      e.preventDefault();
      exec(k === 'b' ? 'bold' : k === 'i' ? 'italic' : 'underline');
    }
  });

  const saveAndClose = async () => {
    const clean = sanitizeNotesHTML(editor.innerHTML);
    await saveNotes(a, clean);
    closeEditor();
  };

  const card = el('div', { class: 'modal notes-modal' }, [
    el('h2', {}, 'Notes'),
    el('p', {}, `${a.title || 'This role'}${a.company_name ? ' · ' + a.company_name : ''}`),
    toolbar,
    editor,
    el('div', { class: 'modal-actions' }, [
      el('button', { class: 'btn ghost', onclick: () => closeEditor() }, 'Cancel'),
      el('button', { class: 'btn', onclick: () => saveAndClose() }, 'Save'),
    ]),
  ]);
  veil.appendChild(card);
  document.body.appendChild(veil);
  // Use <p> paragraphs for Enter (consistent, survives sanitizing) where the
  // browser supports it. Harmless where it doesn't.
  try { document.execCommand('defaultParagraphSeparator', false, 'p'); } catch (_) {}
  editor.focus();
  syncToolbar();
}

async function saveNotes(a, notes) {
  try {
    await api.post('/api/applications/update', {
      company_key: a.company_key, id: a.id, phase_id: a.phase_id, notes,
    });
    a.notes = notes;       // local update
    drawTracker();         // refresh the preview snippet in place
  } catch (e) { toast(e.message, true); }
}

async function removeApplication(a) {
  if (!confirm(`Remove “${a.title || 'this role'}” from your tracker? This doesn’t affect the listing — only your record of applying.`)) return;
  try {
    await api.post('/api/applications/remove', {
      company_key: a.company_key, id: a.id, phase_id: a.phase_id,
    });
    await refreshTracker();
  } catch (e) { toast(e.message, true); }
}

/* Manual add (L.4) — a role the user saw elsewhere. Title + company + URL. */
function openManualAdd() {
  // type=text (not url) so the themed input[type=text] baseline applies — a
  // bare type=url was rendering with browser-default (white) colours.
  const titleI = el('input', { type: 'text', placeholder: 'e.g. Head of Data' });
  const companyI = el('input', { type: 'text', placeholder: 'e.g. Spotify' });
  const urlI = el('input', { type: 'text', placeholder: 'https://… (optional)' });

  const field = (labelText, input) =>
    el('div', { class: 'field' }, [ el('label', {}, labelText), input ]);

  const submit = async () => {
    const title = titleI.value.trim();
    const company = companyI.value.trim();
    if (!title) { toast('A job title, at least.', true); titleI.focus(); return; }
    if (!company) { toast('Which company?', true); companyI.focus(); return; }
    try {
      await api.post('/api/applications/add', {
        company_name: company,
        job: { title, url: urlI.value.trim() },
      });
      veil.remove();
      toast('Added to your tracker.');
      await refreshTracker();
    } catch (e) { toast(e.message, true); }
  };
  // Enter submits from any field.
  [titleI, companyI, urlI].forEach(i =>
    i.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }));

  const veil = el('div', { class: 'modal-veil', onclick: (e) => { if (e.target === veil) veil.remove(); } });
  const card = el('div', { class: 'modal' }, [
    el('h2', {}, 'Add a role you applied to'),
    el('p', {}, 'For a role you saw elsewhere. It’s recorded under the current phase, dated today, as “Applied”.'),
    field('Job title', titleI),
    field('Company', companyI),
    field('Listing link', urlI),
    el('div', { class: 'modal-actions' }, [
      el('button', { class: 'btn ghost', onclick: () => veil.remove() }, 'Cancel'),
      el('button', { class: 'btn', onclick: submit }, 'Add'),
    ]),
  ]);
  veil.appendChild(card);
  document.body.appendChild(veil);
  titleI.focus();
}

async function refreshTracker() {
  try {
    TRACKER = await api.get('/api/applications');
  } catch (e) { toast(e.message, true); return; }
  drawTracker();
}

/* =======================================================================
   GUIDE (Phase J) — "Add a custom (Tier-3) board" inspection guide.
   The honest reality (handover §5/§12): a fully-custom careers board can't be
   added by filling in a form — a reader (connector) has to be built in code.
   That build is a COLLABORATION: YOU do the in-browser capture (only your real
   machine sees the real requests), an agent writes the connector from it. This
   page is YOUR half — the exact, proven inspection that cracked Apple and Google
   (§12 Steps 1–4) — plus a form that turns your findings into a ready-to-hand-
   over markdown brief you download and give to an agent.

   Deliberately NOT built (per the user, this session): no request queue, no
   companies-list entry, no server store. Just the guide + the .md export. And
   honestly stated on the page: the app cannot install the connector itself —
   that needs new code + a restart (§12 Steps 5–7, the agent's half).
   ======================================================================= */

async function renderGuide() {
  const view = $('#view');

  // --- the fillable fields (findings the agent needs; §12 Steps 1–4) -------
  const f = {};
  const field = (id, label, hint, placeholder, multiline) => {
    const input = multiline
      ? el('textarea', { id, placeholder, rows: multiline === true ? 4 : multiline })
      : el('input', { type: 'text', id, placeholder });
    f[id] = input;
    return el('div', { class: 'field' }, [
      el('label', { for: id }, label),
      hint ? el('p', { class: 'pref-hint', style: 'margin:2px 0 8px' }, hint) : null,
      input,
    ]);
  };

  const companyField = field('g_company', 'Company name', null, 'e.g. Figma');
  const careersField = field('g_careers', 'Careers page URL', 'The page that lists the jobs.', 'https://…/careers');
  // If we arrived here from a Tier-3 hit in Add-a-Company, pre-fill the URL.
  if (GUIDE_PREFILL_URL) { f.g_careers.value = GUIDE_PREFILL_URL; GUIDE_PREFILL_URL = null; }
  const jobsReqField  = field('g_jobsreq', 'The jobs request URL', 'Step 2–3: in Network → Fetch/XHR, the request that fires when the jobs load/change (named like search, jobs, api, graphql, batchexecute). Copy its full Request URL.', 'https://…/api/…', true);
  const methodField   = field('g_method', 'Method (GET or POST)', 'Step 3: shown on the Headers tab.', 'GET or POST');
  const bodyField     = field('g_body', 'Request body (if POST)', 'Step 3: the Request Data at the bottom of Headers. Leave blank for GET. If it’s huge, note that and keep the file to hand instead.', 'paste the POST body, or leave blank', true);
  const tokenField    = field('g_token', 'Any token / login step?', 'Step 4: did a separate small request (named token/csrf/session/config) fire first? Apple’s search needed a CSRF token from a /CSRFToken request, returned in a RESPONSE header. Note anything like that, or write “none seen”.', 'e.g. a /CSRFToken request returned x-...-csrf-token header — or “none seen”', true);
  const sampleField   = field('g_sample', 'A sample of the RESPONSE BODY ★ most important', 'Step 3–4: THE field that makes a brief buildable. Click the jobs request → the Response/Preview tab (NOT Headers) → copy a chunk of the JSON that actually comes back (the part with job titles, locations and links in it). Headers alone aren’t enough — without the response body an agent can’t see where the city/title/id live, and the build stalls. If your browser hides the Response tab (Safari can), paste the request URL straight into the address bar and copy the JSON it shows. A scrambled prefix like )]}\u2019 is worth noting — Google had one.', 'paste a slice of the actual JSON RESPONSE (one or two whole job entries is plenty)', 6);

  // --- Part-1-lesson fields (the things that bit us on Apple & Google) ------
  // These four are the difference between a brief that LOOKS complete and one an
  // agent can actually build a correct connector from first time. Each maps to a
  // real bug we hit: Google's city hidden in a field we ignored; Apple's 2000-cap
  // pagination; Apple's multi-site duplicate rows; the global-board scoping need.
  const cityField     = field('g_city', 'Where is the CITY in the response?', 'THE most important question (it’s what bit Google). In your sample above, find where the town/city sits — e.g. a "city" field, or inside a location list, or part of a "London, UK" string. Note the field name / path. If you only see a COUNTRY (e.g. "GB" or "United Kingdom") and no city, say so — that’s a known hard case.', 'e.g. each role has a locations[] list; the city is locations[0].city — or “only a country code, no city”', true);
  const pageField     = field('g_pagination', 'How do you get MORE than the first page?', 'Apple silently capped at 2000 roles; Google truncated. Click to page 2 (or scroll) and watch the request repeat — what CHANGES? A page number / offset / cursor in the URL or body? Does the response say a TOTAL count anywhere? Roughly how many roles per page? Note all you can — the agent needs this to fetch EVERYTHING, not just page 1.', 'e.g. body has "page":N; response has "totalRecords":840; ~20 per page', true);
  const dupField      = field('g_multiloc', 'Does a role REPEAT, or list MULTIPLE locations?', 'Apple lists a multi-site role once PER location (same job, 3 rows: Cambridge / London / Saint Albans). Skim your sample: does the same job id/title appear more than once? Or does one role carry several locations at once? Either way, note it — the connector must merge them, not double-count.', 'e.g. yes — same role appears once per office; OR each role has a list of locations; OR “no repeats seen”', true);
  const globalField   = field('g_global', 'Is this a GLOBAL board (returns the whole world)?', 'Apple/Google return thousands of roles across every country, so JobWatch scopes the fetch by country at the source. Does this board return roles from many countries at once (big global employer), or just a handful of offices? If global, note whether the request can be told a country/location (a filter param you saw in the URL/body).', 'e.g. global — the body takes a "locations":["postLocation-GBR"] filter; OR “small, UK-only, no scoping needed”', true);

  const notesField    = field('g_notes', 'Anything else — or anything you couldn’t find', 'The catch-all. Footer “powered by ___”, a country picker, jobs appearing a beat after load (JavaScript-rendered), roughly how many roles — all help. **Crucially, note anything you looked for but COULDN’T find** (e.g. “couldn’t spot a total count”, “no city anywhere, only country”) so the agent knows what’s uncertain rather than guessing.', 'free notes — include anything you were unsure about or couldn’t locate', true);

  // --- build + download the markdown brief ---------------------------------
  function buildMarkdown() {
    const v = (id) => (f[id].value || '').trim();
    const company = v('g_company') || 'Unnamed company';
    const today = new Date().toISOString().slice(0, 10);
    // An agent-ready brief: the captured findings (Steps 1–4) PLUS a build spec,
    // a JobWatch build checklist (the Apple/Google lessons, baked in), and the
    // exact list of app files to hand over so the agent can actually integrate.
    return [
      `# Connector build brief — ${company}`,
      ``,
      `_Captured ${today} with the JobWatch inspection guide. Build patterns below are baked from the proven Apple & Google connectors._`,
      `_Capture half (Steps 1–4) is below. The build half (write the connector, wire it in, verify) is the agent's — follow the checklist in “How to build this in JobWatch”._`,
      ``,
      `## Company`,
      `- **Name:** ${company}`,
      `- **Careers page:** ${v('g_careers') || '(not given)'}`,
      ``,
      `## The jobs request (Step 2–3)`,
      `- **Request URL:** ${v('g_jobsreq') || '(not given)'}`,
      `- **Method:** ${v('g_method') || '(not given)'}`,
      `- **Request body (POST):**`,
      '```',
      v('g_body') || '(none / GET)',
      '```',
      ``,
      `## Security mechanisms (Step 4)`,
      `- **Token / login / cookie step:** ${v('g_token') || '(not given)'}`,
      ``,
      `## Sample response body (Step 3–4) — the field the build depends on`,
      '```',
      v('g_sample') || '(NOT GIVEN — ⚠ the agent likely CANNOT build this connector without a sample of the actual JSON response body. Ask for it before starting: open the jobs request → Response/Preview tab → copy one or two whole job entries.)',
      '```',
      ``,
      `## Location, pagination & shape (Phase O Part 1 lessons — the things that bit Apple & Google)`,
      `- **Where the CITY lives in the response:** ${v('g_city') || '(not given — agent MUST locate this before building; a country-only connector is the Google bug)'}`,
      `- **Pagination (how to fetch beyond page 1 + any total count):** ${v('g_pagination') || '(not given — agent MUST handle this; the Apple bug was a silent page cap)'}`,
      `- **Repeats / multiple locations per role:** ${v('g_multiloc') || '(not given)'}`,
      `- **Global board / source-side scoping:** ${v('g_global') || '(not given)'}`,
      ``,
      `## Other observations & unknowns`,
      v('g_notes') || '(none)',
      ``,
      `## How to build this in JobWatch (agent checklist)`,
      `Work through every item; mark the ones that apply. These are baked from the`,
      `proven Apple & Google connector builds so the same bugs aren't repeated.`,
      ``,
      `**1. Return the locked job shape.** Use the shared \`_job(id_, title, location, department, url)\` helper in \`connectors.py\` — never hand-build the dict. \`id\` must be a STABLE unique string (drives new/removed detection). \`department\` is \`""\` when absent. (Shape: \`DATA_FORMATS.md\` §1.)`,
      `**2. Capture the CITY, not just the country (the Google lesson).** Read the city from wherever the capture above found it. Do NOT settle for a country code if a city exists in the response — the connector that reads the wrong field is exactly the bug Phase O fixed. \`location\` should contain the city so \`filters.filter_by_location\` matches it. If the board genuinely only exposes a country, that's allowed — the filter's country fallback handles it — but say so explicitly.`,
      `**3. Fetch EVERYTHING — robust pagination (the Apple lesson).** Page to the board's own reported TOTAL (not an arbitrary cap). RETRY a failed page once before stopping (a single failed page must never silently truncate). If you must stop short, surface "fetched N of M", don't return short silently.`,
      `**4. Merge duplicate / multi-site rows.** If the same role id appears more than once (e.g. once per office) or a role lists several locations, collapse it with the shared \`_merge_by_id(jobs)\` helper in \`connectors.py\` — it keeps one record and MERGES the locations ("Cambridge; London; Saint Albans"). Double-counting distorts new/removed detection.`,
      `**5. If it's a GLOBAL board, scope the fetch at the source.** Add the connector to \`SCOPABLE\` in \`market_scope.py\` and map the user's chosen cities → the board's country filter (Apple uses \`postLocation-<ISO3>\`, Google uses ISO-2). \`geo.region_for_city\` + the \`COUNTRY_ISO\` map already turn a city into its codes. A small/regional board needs none of this — skip it.`,
      `**6. Register the connector.** Add it to the \`CONNECTORS\` dict in \`connectors.py\` as \`(func, [required_config_keys], "human description")\`, and add a \`_detect_<name>\` recogniser to \`detect.py\` + its \`_RECOGNISERS\` entry so paste-URL auto-detect works (otherwise it can only be added by hand).`,
      `**7. No personal cookies.** The public job list works logged-out; the connector must use none (see the Connector rules below). Reuse \`BROWSER_HEADERS\` / \`http_get\` / \`_polite_pause\` from \`connectors.py\` for safe, paced requests.`,
      `**8. Verify live.** The sandbox can't reach the board — the user runs the connector on their Mac and eyeballs the count against the careers page. That real count is the checkpoint.`,
      ``,
      `## Connector rules (must follow — distilled from the proven Apple/Google build)`,
      `- **Connectors target PLATFORMS, not companies.** A reader reads one job-board *platform's* feed (Greenhouse handles every Greenhouse company). Before building anything custom, rule out the easy path: if the careers URL contains \`greenhouse\` / \`lever\` / \`ashby\` / \`workable\` / \`smartrecruiters\` or looks like Workday/Eightfold, it's already supported — just add it via paste-URL, no connector needed.`,
      `- **NO personal cookies, ever.** The public job list works logged-out. Never read, store, or hardcode the user's session cookies. If a board *truly* requires login to see jobs, stop and flag it — don't store credentials. (If a board needs a fresh anonymous cookie, GET the main page first with a \`cookiejar\` + \`build_opener\`, like Apple's connector does — that's fine; personal login cookies are not.)`,
      `- **Don't guess endpoints — build against the captured response.** The sample above is reality; parse against it, not against an assumed shape. (That guessing is exactly what wasted rounds on Apple/Google before inspection.)`,
      `- **Be polite by default.** Reuse \`BROWSER_HEADERS\` / \`http_get\` / \`_polite_pause\` from \`connectors.py\`; the public list is hit once every few days, so per-request politeness is enough — no heavy rate-limiting needed.`,
      `- **Custom boards are FRAGILE.** Big bespoke sites reshape their internal endpoints without notice; when one breaks, re-capture with this guide and rebuild. Standard-platform connectors rarely break — prefer them where possible.`,
      ``,
      `## Files to upload WITH this brief (so the agent has everything)`,
      `Hand these over from the app's \`jobwatch/\` folder alongside this brief:`,
      `- **\`connectors.py\`** — WRITE here: the new connector function + its \`CONNECTORS\` entry; also holds \`_job\`, \`_merge_by_id\`, \`BROWSER_HEADERS\`, \`http_get\`, \`_polite_pause\` to reuse.`,
      `- **\`detect.py\`** — WRITE here: the \`_detect_<name>\` recogniser + \`_RECOGNISERS\` entry (for paste-URL auto-detect).`,
      `- **\`market_scope.py\`** — WRITE here ONLY if the board is global (add to \`SCOPABLE\` + scope mapping).`,
      `- **\`geo.py\`** — READ-only reference: \`region_for_city\` + the \`COUNTRY_ISO\` map (city → country codes for scoping).`,
      `- **\`filters.py\`** — READ-only: the location-filter CONTRACT the connector must satisfy (return the city in \`location\`). Do not modify.`,
      `- **\`DATA_FORMATS.md\`** — READ-only: the locked job/company shapes the connector must return.`,
      `- _(If the POST body or response sample was too big to paste above, attach it as a file too.)_`,
      ``,
      `Everything the agent needs to build this connector is in THIS brief plus the files above — no other project context is required.`,
      ``,
      `## Safety note`,
      `- This brief should contain **no personal login cookies**. The public job`,
      `  list works logged-out; the connector must use no personal cookies.`,
      ``,
    ].join('\n');
  }

  function downloadBrief() {
    const company = (f.g_company.value || 'company').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'company';
    if (!f.g_careers.value.trim() && !f.g_jobsreq.value.trim()) {
      toast('Add at least the careers URL or the jobs request before downloading.', true);
      return;
    }
    const md = buildMarkdown();
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = el('a', { href: url, download: `${company}-Jobwatch.md` });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (!f.g_sample.value.trim()) {
      toast('Downloaded — but no response body was captured. The build may stall without it; consider adding the JSON response and re-downloading.', true);
    } else {
      toast('Brief downloaded. Hand it to an agent to build the connector.');
    }
  }

  const children = [
    el('div', { class: 'sec-head' }, [ el('h2', {}, 'Add a custom board') ]),
    el('p', { class: 'lead' },
      'Most companies add straight from their URL — just use “Add a company”, which detects the board for you. This page is for the harder ones: a fully custom careers site with no standard board behind it. Those can’t be added by a setting — a reader has to be built in code. You capture what it needs here; an agent builds it from your notes.'),

    // The honest split of labour.
    el('div', { class: 'help-card' }, [
      el('p', { class: 'help-lead' },
        'Why it works this way: only your own browser can see the real request a custom site uses to load its jobs. You inspect the page and note a few things; an agent turns that into a connector and adds it to the app (which needs new code and a restart — the app can’t install a connector into itself). This is exactly how Apple and Google were added.'),
    ]),

    // The inspection steps (§12 Steps 1–4) — the part YOU do.
    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'What to inspect (≈10 minutes)'),
      el('ol', { class: 'guide-steps' }, [
        el('li', {}, [ el('b', {}, 'Open the careers page and the inspector. '),
          'In Chrome: right-click → Inspect → Network tab. In Safari: enable Develop (Settings → Advanced → “Show features for web developers”), then Cmd+Option+I → Network. Filter to ', el('b', {}, 'Fetch/XHR'), '.' ]),
        el('li', {}, [ el('b', {}, 'Make the jobs load. '),
          'Clear the Network list, then reload the page or search/click to page 2. Watch for the request that fires and returns a chunk of JSON with job titles in it — named something like ', el('code', { class: 'guide-frag' }, 'search'), ', ', el('code', { class: 'guide-frag' }, 'jobs'), ', ', el('code', { class: 'guide-frag' }, 'api'), ', ', el('code', { class: 'guide-frag' }, 'graphql'), ', or ', el('code', { class: 'guide-frag' }, 'batchexecute'), '. Not the .js / .css / image rows.' ]),
        el('li', {}, [ el('b', {}, 'Read three things off its Headers tab: '),
          'the full Request URL, the Method (GET or POST), and the Request body if it’s a POST.' ]),
        el('li', {}, [ el('b', {}, 'Watch for a token or login step. '),
          'Did a small request (token / csrf / session / config) fire first? Check its response headers too, not just the body — Apple’s CSRF token arrived in a response header. Note anything like that.' ]),
        el('li', {}, [ el('b', {}, 'Copy a slice of the RESPONSE BODY — the most important step. '),
          'Click the jobs request, open the ', el('b', {}, 'Response'), ' (or Preview) tab — ', el('b', {}, 'not'), ' Headers — and copy a chunk of the JSON it returns (enough to show one or two whole job entries, with the title, location and link). This is what an agent builds from; a brief without it can’t be built. ',
          el('b', {}, 'Safari tip: '), 'if no Response tab shows, paste the request URL straight into the address bar and copy the JSON the page prints.' ]),
        el('li', {}, [ el('b', {}, 'Find the CITY in that response. '),
          'This is the one that bit Google — its city sat in a field the old reader ignored, so every role looked country-only. In your sample, locate the town/city for a role and note where it is (a ', el('code', { class: 'guide-frag' }, 'city'), ' field? inside a locations list? part of a “London, UK” string?). If you only see a country, say so.' ]),
        el('li', {}, [ el('b', {}, 'Work out how to get MORE pages. '),
          'Apple silently capped at 2000 roles. Click to page 2 (or scroll) and watch the request repeat — note what changes (a page number / offset / cursor) and whether the response states a ', el('b', {}, 'total count'), '. The agent needs this to fetch everything.' ]),
        el('li', {}, [ el('b', {}, 'Check for repeats or multi-location roles. '),
          'Apple lists a multi-site role once per office (same job, three rows). Skim the sample: does a job id/title repeat, or does one role carry several locations? Note it so the connector merges instead of double-counting.' ]),
      ]),
      el('p', { class: 'pref-hint' },
        'One safety rule: don’t copy any personal login cookies into your notes. The public job list works logged-out, and the connector is built to use none.'),
    ]),

    // The fillable brief — the part that produces the .md.
    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Record what you found'),
      el('p', { class: 'pref-hint' },
        'Fill in whatever you captured — blanks are fine, but the more of the location/pagination ones you complete, the better the connector an agent can build first time. Download turns it into a markdown brief you hand to an agent.'),
      companyField, careersField, jobsReqField, methodField, bodyField, tokenField, sampleField,
      cityField, pageField, dupField, globalField, notesField,
      el('div', { class: 'btn-row', style: 'margin-top:18px' }, [
        el('button', { class: 'btn signal', onclick: downloadBrief }, 'Download brief (.md)'),
        el('button', { class: 'btn ghost', onclick: openAddCompany }, 'Back to adding a company'),
      ]),
    ]),

    // What to hand over alongside the brief — the file list, mirrored from the .md.
    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'What to hand over with the brief'),
      el('p', { class: 'pref-hint' },
        'The brief alone tells the agent WHAT to build. To let it actually build and wire the connector in, also upload these files from your app’s ', el('code', { class: 'guide-frag' }, 'jobwatch/'), ' folder. (The downloaded brief lists them too, so nothing is lost if you hand it on later.)'),
      el('ul', { class: 'guide-steps' }, [
        el('li', {}, [ el('b', {}, 'connectors.py'), ' — where the new connector + its registry entry are written; also holds the shared ', el('code', { class: 'guide-frag' }, '_job'), ' and ', el('code', { class: 'guide-frag' }, '_merge_by_id'), ' helpers to reuse.' ]),
        el('li', {}, [ el('b', {}, 'detect.py'), ' — where the paste-URL auto-detect recogniser is added.' ]),
        el('li', {}, [ el('b', {}, 'market_scope.py'), ' — only needed if it’s a big global board that should fetch by country.' ]),
        el('li', {}, [ el('b', {}, 'geo.py'), ' · ', el('b', {}, 'filters.py'), ' · ', el('b', {}, 'DATA_FORMATS.md'), ' — read-only references (location resolution, the filter contract, the job shape).' ]),
      ]),
      el('p', { class: 'pref-hint', style: 'margin-top:8px' },
        'All of these live in your app folder. Find them once, keep them handy — the same set works for every new company. The brief itself carries all the rules and patterns, so nothing else from the project is needed.'),
    ]),
  ];

  view.replaceChildren(...children);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* =======================================================================
   LAST REPORT (G.9) — revisit the most recent run's report without re-running.
   Prefers this session's in-memory result; falls back to the saved copy on
   disk (which survives an app restart, per the user's choice). Read-only.
   ======================================================================= */

async function openLastReport() {
  REPORT_VIEW = 'company';
  if (LAST_RESULT) { drawReport(LAST_RESULT, true); return; }
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Finding your last results…'));
  let data;
  try {
    data = await api.get('/api/last-report');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t open your last results.', e.message));
    return;
  }
  if (!data || !data.result) {
    $('#view').replaceChildren(
      el('div', { class: 'sec-head' }, [ el('h2', {}, 'Latest results') ]),
      el('div', { class: 'empty' }, [
        el('h3', {}, 'No results yet'),
        el('p', {}, 'Once you check a bucket, its report shows here — and stays reachable afterwards, so you can come back to the last run without checking again.'),
        el('button', { class: 'btn signal', onclick: () => go('home') }, 'Back to home'),
      ]),
    );
    return;
  }
  LAST_RESULT = data.result;
  drawReport(LAST_RESULT, true);
}


function plural(n, one, many) { return n === 1 ? one : many; }

/* =======================================================================
   RUN — live progress via Server-Sent Events
   ======================================================================= */

async function startRun(bucket) {
  try {
    await api.post('/api/run/start', { bucket });
  } catch (e) {
    toast(e.message, true);
    return;
  }
  renderRunPanel(bucket);
  streamRun();
}

/* Update the per-company (second) bar: company name + stage + a fill that
   advances fetching -> filtering -> saving. Pure UI; driven by company_stage
   events from the engine. */
const _STAGE_INFO = {
  starting:  { pct: 8,   label: 'starting…' },
  fetching:  { pct: 38,  label: 'fetching roles…' },
  filtering: { pct: 70,  label: 'filtering & flagging…' },
  saving:    { pct: 100, label: 'saving…' },
};
function updateCompanyBar(name, stage) {
  const nameEl = $('#coBarName');
  const stageEl = $('#coBarStage');
  const fill = $('#coBarFill');
  if (!nameEl || !fill) return;
  const info = _STAGE_INFO[stage] || { pct: 0, label: stage || '' };
  if (name) nameEl.textContent = name;
  if (stageEl) stageEl.textContent = info.label;
  fill.style.width = info.pct + '%';
}

function clearCompanyBar() {
  const nameEl = $('#coBarName');
  const stageEl = $('#coBarStage');
  const fill = $('#coBarFill');
  if (nameEl) nameEl.textContent = '';
  if (stageEl) stageEl.textContent = '';
  if (fill) fill.style.width = '0%';
}

function renderRunPanel(bucket) {
  $('#view').replaceChildren(
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
    el('div', { class: 'sec-head' }, [
      el('h2', {}, `Checking “${bucket}”`),
      el('button', { class: 'btn ghost small danger', id: 'cancelRunBtn',
                     onclick: cancelRun }, 'Cancel check'),
    ]),
    el('div', { class: 'run-panel', id: 'runPanel' }, [
      el('p', { class: 'run-note', id: 'runNote' }, 'Starting…'),
      // Overall bar — progress across all companies in the run.
      el('div', { class: 'bar-block' }, [
        el('div', { class: 'bar-caption' }, 'Overall'),
        el('div', { class: 'bar-track' }, [ el('div', { class: 'bar-fill', id: 'barFill' }) ]),
        el('div', { class: 'bar-label' }, [
          el('span', { id: 'barText' }, 'Preparing'),
          el('span', { id: 'barPct' }, ''),
        ]),
      ]),
      // Per-company bar — what's happening for the company being checked now.
      el('div', { class: 'bar-block', id: 'coBarBlock' }, [
        el('div', { class: 'bar-caption' }, [
          el('span', { id: 'coBarName' }, 'Waiting to start…'),
          el('span', { class: 'co-stage', id: 'coBarStage' }, ''),
        ]),
        el('div', { class: 'bar-track' }, [ el('div', { class: 'bar-fill co', id: 'coBarFill' }) ]),
      ]),
      el('ul', { class: 'run-list', id: 'runList' }),
    ]),
  );
}

/* Cooperative cancel: ask the server to stop the run at the next
   between-company checkpoint. The stream then delivers a run_cancelled event
   followed by the partial result, so whatever was checked is still shown. */
async function cancelRun() {
  const btn = $('#cancelRunBtn');
  if (btn) { btn.setAttribute('disabled', ''); btn.textContent = 'Stopping…'; }
  try {
    await api.post('/api/run/cancel', {});
    const note = $('#runNote');
    if (note) note.textContent = 'Stopping after the current company finishes…';
  } catch (e) { toast(e.message, true); if (btn) { btn.removeAttribute('disabled'); btn.textContent = 'Cancel check'; } }
}

function streamRun() {
  const source = new EventSource('/api/run/stream');
  const list = $('#runList');
  const rows = {}; // key -> <li>
  let total = 0, doneCount = 0;

  const setBar = () => {
    const pct = total ? Math.round((doneCount / total) * 100) : 0;
    $('#barFill').style.width = pct + '%';
    $('#barPct').textContent = pct + '%';
    $('#barText').textContent = `${doneCount} of ${total} checked`;
  };

  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }

    switch (ev.kind) {
      case 'run_start':
        total = ev.total || 0;
        $('#runNote').textContent = ev.note || '';
        $('#barText').textContent = `0 of ${total} checked`;
        $('#barPct').textContent = '0%';
        break;

      case 'company_start': {
        const li = el('li', {}, [
          el('span', { class: 'tick doing' }, '○'),
          el('span', { class: 'r-name' }, ev.name),
          el('span', { class: 'r-delta', id: `d-${ev.key}` }, 'checking…'),
        ]);
        rows[ev.key] = li;
        list.appendChild(li);
        updateCompanyBar(ev.name, 'starting');
        break;
      }

      case 'company_stage':
        updateCompanyBar(ev.name, ev.stage);
        break;

      case 'company_done': {
        const li = rows[ev.key];
        if (li) {
          const tick = li.firstChild;
          const delta = $('#d-' + ev.key, li) || li.lastChild;
          if (ev.skipped) {
            tick.className = 'tick skipped'; tick.textContent = '–';
            delta.textContent = 'not yet supported';
          } else if (ev.ok) {
            tick.className = 'tick done'; tick.textContent = '✓';
            delta.replaceChildren(
              ev.new_count
                ? el('span', { class: 'new' }, `${ev.new_count} new`)
                : document.createTextNode('no change'),
              document.createTextNode(ev.removed_count ? ` · ${ev.removed_count} gone` : ''),
            );
          } else {
            tick.className = 'tick failed'; tick.textContent = '!';
            delta.textContent = 'couldn’t reach — skipped';
          }
        }
        doneCount++;
        setBar();
        break;
      }

      case 'run_failed':
        source.close();
        $('#runNote').textContent = '';
        $('#view').replaceChildren(
          el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
          errorBox('That check couldn’t run.', ev.message),
        );
        break;

      case 'result':
        LAST_RESULT = ev.result;
        break;

      case 'end':
        source.close();
        // Refresh home state (reading/last-check changed), then show the report
        // as its own route so the banner appears and a refresh keeps it (G.6).
        api.get('/api/home').then(h => { HOME = h; }).catch(() => {}).finally(() => {
          if (LAST_RESULT) {
            $('#barFill').style.width = '100%';
            $('#barPct').textContent = '100%';
            setTimeout(() => go('report'), 350);
          } else {
            go('home');
          }
        });
        break;

      case 'idle':
        source.close();
        loadHome();
        break;
    }
  };

  source.onerror = () => {
    // The stream closes itself on 'end'; only surface a real mid-run drop.
    if (!LAST_RESULT) {
      source.close();
      toast('Lost contact with the check. Try again.', true);
      loadHome();
    }
  };
}

/* =======================================================================
   REPORT — two views over one result
   ======================================================================= */

function renderReport(result) {
  REPORT_VIEW = 'company';
  drawReport(result, true);
}

function drawReport(result, scrollTop = false) {
  const view = $('#view');
  const counts = result.counts || {};
  const children = [
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
  ];

  // headline
  children.push(el('div', { class: 'report-head' }, [
    el('div', {}, [
      el('div', { class: 'sec-head', style: 'margin:0' }, [
        el('h2', {}, result.bucket),
      ]),
      el('div', { class: 'tally' }, [
        el('b', {}, `${counts.total_new ?? 0} new`),
        document.createTextNode(
          ` · ${counts.total_removed ?? 0} removed · ` +
          `${counts.companies_checked ?? 0} checked` +
          (counts.companies_failed ? ` · ${counts.companies_failed} unreachable` : '') +
          (counts.companies_skipped ? ` · ${counts.companies_skipped} not yet supported` : '')
        ),
      ]),
    ]),
    el('div', { class: 'toggle' }, [
      el('button', { class: REPORT_VIEW === 'company' ? 'on' : '', onclick: () => { REPORT_VIEW = 'company'; drawReport(result); } }, 'By company'),
      el('button', { class: REPORT_VIEW === 'all' ? 'on' : '', onclick: () => { REPORT_VIEW = 'all'; drawReport(result); } }, 'All new roles'),
    ]),
  ]));

  // Phase L.10 — "a role you applied to is gone". Calm heads-up for any tracked,
  // still-live application whose role vanished from what this run just saw. Shown
  // above the new/removed detail, and even when nothing else moved (a vanished
  // application is exactly the signal worth surfacing on a quiet day).
  const gone = result.gone_alerts || [];
  if (gone.length) {
    children.push(el('div', { class: 'gone-alert' }, [
      el('div', { class: 'gone-head' },
        gone.length === 1
          ? 'Heads up — a role you applied to is no longer listed:'
          : `Heads up — ${gone.length} roles you applied to are no longer listed:`),
      el('ul', { class: 'gone-list' }, gone.map(g =>
        el('li', {}, [
          g.url
            ? el('a', { href: g.url, target: '_blank', rel: 'noopener' }, g.title || '(untitled role)')
            : document.createTextNode(g.title || '(untitled role)'),
          el('span', { class: 'muted' }, ` · ${g.company_name || ''}`),
        ]))),
      el('div', { class: 'gone-foot muted' },
        'It’s still in your tracker — update its status there when you know more.'),
    ]));
  }

  if ((counts.total_new ?? 0) === 0 && (counts.total_removed ?? 0) === 0) {
    children.push(el('div', { class: 'empty' }, [
      el('h3', {}, 'Nothing moved'),
      el('p', {}, 'No new or removed roles since the last check. That’s the quiet, expected result most days — the signal matters when it’s here.'),
    ]));
    view.replaceChildren(...children);
    return;
  }

  if (REPORT_VIEW === 'company') {
    children.push(...renderByCompany(result));
  } else {
    children.push(renderAllNew(result));
  }
  view.replaceChildren(...children);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderByCompany(result) {
  const blocks = [];
  for (const c of result.companies || []) {
    if (c.skipped) continue;
    const newJobs = c.new || [];
    const removed = c.removed || [];
    if (!newJobs.length && !removed.length && c.ok) continue; // hide quiet companies

    const countLabel = c.baseline
      ? `baseline · ${newJobs.length} recorded`
      : `${newJobs.length} new` + (removed.length ? ` · ${removed.length} removed` : '');

    const block = el('div', { class: 'company-block' }, [
      el('h3', {}, [
        document.createTextNode(c.name),
        el('span', { class: 'c-count' }, c.ok ? countLabel : 'couldn’t reach'),
      ]),
    ]);
    newJobs.forEach(j => block.appendChild(jobRow(j, 'new')));
    removed.forEach(j => block.appendChild(jobRow(j, 'removed')));
    blocks.push(block);
  }
  if (!blocks.length) {
    return [el('div', { class: 'empty' }, [
      el('h3', {}, 'Nothing to show by company'),
      el('p', {}, 'Every company came back unchanged.'),
    ])];
  }
  return blocks;
}

function renderAllNew(result) {
  const all = result.all_new || [];
  if (!all.length) {
    return el('div', { class: 'empty' }, [
      el('h3', {}, 'No new roles across the bucket'),
      el('p', {}, 'Switch to “By company” to see removals, if any.'),
    ]);
  }
  const block = el('div', { class: 'company-block' });
  all.forEach(j => block.appendChild(jobRow(j, 'new', true)));
  return block;
}

/* M — the shared flag tags for a role. Used by jobRow (run/current views) and
   now by savedRow + trackerRow (re-flagged on read), so flags look identical
   everywhere. `opts.includeNew` adds the 'new' pill (run view only). Reuses the
   existing .tag.* CSS classes — no new styles invented. */
function flagTags(job, opts = {}) {
  const tags = [];
  if (opts.includeNew && opts.kind === 'new') tags.push(el('span', { class: 'tag new' }, 'new'));
  if (typeof job.interest_rank === 'number') {
    const hits = (job.interest_hits || []).join(', ');
    tags.push(el('span', { class: 'tag interest' }, `#${job.interest_rank + 1}${hits ? ' ' + hits : ''}`));
  }
  if (job.location_unclear) tags.push(el('span', { class: 'tag unclear' }, 'location unclear'));
  if (job.location_country_only) tags.push(el('span', { class: 'tag unclear' }, 'city not specified'));
  if (job.department_unclear) tags.push(el('span', { class: 'tag unclear' }, 'department unclear'));
  if (job.department_match) tags.push(el('span', { class: 'tag interest' }, 'dept match'));
  if (job.experience_stretch) tags.push(el('span', { class: 'tag stretch' }, `${job.experience_required}+ yrs`));
  return tags;
}

function jobRow(job, kind, showCompany = false, ctx = null) {
  const sub = [];
  if (showCompany && job._company) sub.push(el('span', { class: 'j-company' }, job._company));
  if (job.location) sub.push(el('span', {}, job.location));
  if (job.department) sub.push(el('span', {}, job.department));

  const tags = flagTags(job, { includeNew: true, kind });

  const titleNode = job.url
    ? el('a', { href: job.url, target: '_blank', rel: 'noopener' }, job.title || '(untitled role)')
    : document.createTextNode(job.title || '(untitled role)');

  // Star (Phase K): on every role across the Jobs page. ctx carries the company
  // this role belongs to so a save knows where to file it. The star only appears
  // on the Jobs page itself — the old G.9 'report' route (reached from Home) is
  // left as it was. When ctx is null and the role has no _company_key, no star.
  const onJobsPage = CURRENT_ROUTE === 'jobs';
  const companyKey = ctx ? ctx.key : (job._company_key || null);
  const companyName = ctx ? ctx.name : (job._company || '');
  const starNode = (onJobsPage && companyKey) ? starControl(job, companyKey, companyName) : null;

  return el('div', { class: `job is-${kind}` }, [
    el('span', { class: 'marker' }),
    el('div', { class: 'j-main' }, [
      el('div', { class: 'j-title' }, [titleNode]),
      sub.length ? el('div', { class: 'j-sub' }, sub) : null,
    ]),
    tags.length ? el('div', { class: 'j-sub', style: 'gap:6px' }, tags) : null,
    starNode,
  ]);
}

/* =======================================================================
   PHASE actions (start / switch / end) — modals
   ======================================================================= */

function openPhaseModal(mode) {
  if (mode === 'end') return confirmEndPhase();

  let chosenType = 'active';
  const nameInput = el('input', { type: 'text', placeholder: 'e.g. Spring 2026 hunt', id: 'phaseName' });
  const seg = el('div', { class: 'seg' }, [
    el('button', { class: 'on', id: 'segActive', onclick: () => setType('active') }, [
      'Active hunt', el('span', { class: 'seg-sub' }, 'check every few days'),
    ]),
    el('button', { id: 'segCasual', onclick: () => setType('casual') }, [
      'Casual watch', el('span', { class: 'seg-sub' }, 'check every week or two'),
    ]),
  ]);
  function setType(t) {
    chosenType = t;
    $('#segActive').className = t === 'active' ? 'on' : '';
    $('#segCasual').className = t === 'casual' ? 'on' : '';
  }

  const veil = modal('Start a phase',
    'A phase brackets one stretch of searching. Starting one begins a fresh baseline — your first check records everything as the starting point, with no “removed” noise.',
    [
      el('div', { class: 'field' }, [ el('label', { for: 'phaseName' }, 'Name this phase'), nameInput ]),
      el('div', { class: 'field' }, [ el('label', {}, 'How closely are you watching?'), seg ]),
    ],
    async () => {
      const name = nameInput.value.trim();
      if (!name) { toast('Give the phase a name first.', true); return false; }
      try {
        await api.post('/api/phase/create', { name, type: chosenType });
        toast('Phase started.');
        await loadHome();
        return true;
      } catch (e) { toast(e.message, true); return false; }
    },
    'Start phase');
  document.body.appendChild(veil);
  nameInput.focus();
}

function confirmEndPhase() {
  const veil = modal('End this phase?',
    'Ending a phase is safe and reversible in spirit: nothing is deleted, all your snapshots and history stay browsable. The app simply rests until you start a new phase. Comparison only ever happens within a phase, so this just closes the current chapter.',
    [],
    async () => {
      try {
        await api.post('/api/phase/end', {});
        toast('Phase ended. Resting now.');
        await loadHome();
        return true;
      } catch (e) { toast(e.message, true); return false; }
    },
    'End phase');
  document.body.appendChild(veil);
}

async function switchPhase(toType) {
  try {
    await api.post('/api/phase/switch', { type: toType });
    toast(`Switched to ${toType === 'active' ? 'active hunt' : 'casual watch'}.`);
    await loadHome();
  } catch (e) { toast(e.message, true); }
}

function modal(title, body, fields, onConfirm, confirmLabel) {
  const veil = el('div', { class: 'modal-veil' });
  const close = () => veil.remove();
  veil.addEventListener('click', (e) => { if (e.target === veil) close(); });

  const confirmBtn = el('button', { class: 'btn signal' }, confirmLabel || 'Confirm');
  confirmBtn.addEventListener('click', async () => {
    confirmBtn.disabled = true;
    const ok = await onConfirm();
    if (ok) close(); else confirmBtn.disabled = false;
  });

  veil.appendChild(el('div', { class: 'modal' }, [
    el('h2', {}, title),
    body ? el('p', {}, body) : null,
    ...fields,
    el('div', { class: 'modal-actions' }, [
      el('button', { class: 'btn ghost', onclick: close }, 'Cancel'),
      confirmBtn,
    ]),
  ]));
  return veil;
}

/* ---- shared error box -------------------------------------------------- */
function errorBox(headline, detail) {
  return el('div', { class: 'empty' }, [
    el('h3', {}, headline),
    el('p', {}, detail || 'Something went wrong.'),
    el('button', { class: 'btn ghost', onclick: loadHome }, 'Try again'),
  ]);
}

/* =======================================================================
   MANAGE — the Management Center (Phase I)
   The hub for ALL companies and ALL buckets. Two columns: buckets (left) and
   companies (right). Add-company at the top routes by tier (Tier 3 → an honest
   help page). Companies order three ways; buckets filter the company list;
   buckets create/rename/delete; and buckets opt into SUB-BUCKETS — a same-page
   reorganise where companies drag into named sub-buckets (anything unplaced
   lands in Other/Misc). No code, click-or-drag only.
   ======================================================================= */

let MANAGE = null;                  // last /api/manage payload
let MANAGE_ORDER = 'alpha';         // 'alpha' | 'added' | 'bucketed'
let MANAGE_FILTER = null;           // a bucket name to filter the company list by, or null
let SUBMODE = null;                 // when organising sub-buckets: { bucket, names[], assign:{key:sub} }

async function openManage() {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Loading your companies…'));
  try {
    MANAGE = await api.get('/api/manage');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t load your companies.', e.message));
    return;
  }
  // A filter pointing at a now-deleted bucket should reset.
  if (MANAGE_FILTER && !(MANAGE.buckets || []).some(b => b.name === MANAGE_FILTER)) {
    MANAGE_FILTER = null;
  }
  if (SUBMODE) { drawSubMode(true); } else { drawManage(true); }
}

/* ---- helpers over the manage payload --------------------------------- */

function manageCompanies() { return (MANAGE && MANAGE.companies) || []; }
function manageBuckets()   { return (MANAGE && MANAGE.buckets) || []; }

function sortedCompanies() {
  const list = manageCompanies().slice();
  if (MANAGE_ORDER === 'added') {
    // newest first; missing dates sort last
    list.sort((a, b) => (b.added_on || '').localeCompare(a.added_on || '') ||
                        (a.display_name || '').localeCompare(b.display_name || ''));
  } else if (MANAGE_ORDER === 'bucketed') {
    // in-a-bucket group first, then no-bucket; each alphabetical
    const inB = c => ((c.buckets || []).length ? 0 : 1);
    list.sort((a, b) => inB(a) - inB(b) ||
                        (a.display_name || '').localeCompare(b.display_name || ''));
  } else {
    list.sort((a, b) => (a.display_name || '').localeCompare(b.display_name || ''));
  }
  return list;
}

function visibleCompanies() {
  const list = sortedCompanies();
  if (!MANAGE_FILTER) return list;
  return list.filter(c => (c.buckets || []).includes(MANAGE_FILTER));
}

/* ---- the two-column board -------------------------------------------- */

function drawManage(scrollTop = false) {
  const view = $('#view');
  const companiesAll = manageCompanies();
  const buckets = manageBuckets();

  const children = [
    el('div', { class: 'mc-head' }, [
      el('div', { class: 'sec-head', style: 'margin:0' }, [ el('h2', {}, 'Companies & buckets') ]),
      el('button', { class: 'btn signal', onclick: openAddCompany }, '+ Add a company'),
    ]),
  ];

  // First-run empty state.
  if (!companiesAll.length) {
    children.push(el('div', { class: 'empty' }, [
      el('h3', {}, 'Nothing tracked yet'),
      el('p', {}, 'Add a company by pasting its careers-page URL — JobWatch works out the rest. Then group companies into buckets (labels like “Big Tech” or “London”) so you can check a whole group at once.'),
      el('button', { class: 'btn signal', onclick: openAddCompany }, '+ Add your first company'),
    ]));
    view.replaceChildren(...children);
    return;
  }

  const board = el('div', { class: 'mc-board' }, [
    mcBucketsColumn(buckets),
    mcCompaniesColumn(),
  ]);
  children.push(board);
  view.replaceChildren(...children);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* LEFT — buckets */
function mcBucketsColumn(buckets) {
  const col = el('div', { class: 'mc-col mc-buckets' });
  col.appendChild(el('div', { class: 'mc-col-head' }, [
    el('h3', {}, 'Buckets'),
    el('button', { class: 'btn ghost small', onclick: openCreateBucket }, '+ New bucket'),
  ]));

  if (!buckets.length) {
    col.appendChild(el('p', { class: 'mc-hint' },
      'No buckets yet. A bucket is just a label — make one to group companies you want to check together.'));
    return col;
  }

  // "All companies" pseudo-row clears the filter.
  const allRow = el('button', {
    class: 'mc-bucket-row' + (MANAGE_FILTER === null ? ' on' : ''),
    onclick: () => { MANAGE_FILTER = null; drawManage(); },
  }, [
    el('span', { class: 'mcb-name' }, 'All companies'),
    el('span', { class: 'mcb-count' }, `${manageCompanies().length}`),
  ]);
  col.appendChild(allRow);

  for (const b of buckets) {
    const on = MANAGE_FILTER === b.name;
    const row = el('div', { class: 'mc-bucket-row' + (on ? ' on' : '') });
    // tapping the name filters the company column
    row.appendChild(el('button', {
      class: 'mcb-main',
      onclick: () => { MANAGE_FILTER = on ? null : b.name; drawManage(); },
      title: on ? 'Show all companies' : `Show only “${b.name}”`,
    }, [
      el('span', { class: 'mcb-name' }, b.name),
      el('span', { class: 'mcb-count' }, `${b.company_count}`),
    ]));
    // per-bucket actions: add companies · sub-buckets · rename · delete
    row.appendChild(el('div', { class: 'mcb-actions' }, [
      el('button', { class: 'icon-btn', title: 'Add companies to this bucket',
        onclick: () => openAddToBucket(b.name) }, plusGlyph()),
      el('button', { class: 'icon-btn', title: 'Organise into sub-buckets',
        onclick: () => enterSubMode(b.name) }, subGlyph()),
      el('button', { class: 'icon-btn', title: 'Rename bucket',
        onclick: () => openRenameBucket(b.name) }, penGlyph()),
      el('button', { class: 'icon-btn danger', title: 'Delete bucket',
        onclick: () => confirmDeleteBucket(b.name, b.company_count) }, '×'),
    ]));
    col.appendChild(row);
  }
  return col;
}

/* RIGHT — companies */
function mcCompaniesColumn() {
  const col = el('div', { class: 'mc-col mc-companies' });

  const head = el('div', { class: 'mc-col-head' }, [
    el('h3', {}, MANAGE_FILTER ? `In “${MANAGE_FILTER}”` : 'Companies'),
  ]);
  // order control
  const order = el('div', { class: 'mc-order' }, [
    el('span', { class: 'mc-order-label' }, 'Order'),
    orderBtn('alpha', 'A–Z'),
    orderBtn('added', 'Newest'),
    orderBtn('bucketed', 'In a bucket'),
  ]);
  head.appendChild(order);
  col.appendChild(head);

  if (MANAGE_FILTER) {
    col.appendChild(el('button', { class: 'mc-clear', onclick: () => { MANAGE_FILTER = null; drawManage(); } },
      '← Show all companies'));
  }

  const list = visibleCompanies();
  if (!list.length) {
    col.appendChild(el('div', { class: 'empty small' }, [
      el('p', {}, MANAGE_FILTER
        ? `No companies in “${MANAGE_FILTER}” yet.`
        : 'No companies match.'),
    ]));
    return col;
  }

  const wrap = el('div', { class: 'company-list' });
  for (const c of list) wrap.appendChild(companyRow(c));
  col.appendChild(wrap);
  return col;
}

function orderBtn(key, label) {
  return el('button', {
    class: 'mc-order-btn' + (MANAGE_ORDER === key ? ' on' : ''),
    onclick: () => { MANAGE_ORDER = key; drawManage(); },
  }, label);
}

function companyRow(c) {
  const bucketTags = (c.buckets || []).length
    ? c.buckets.map(b => el('span', { class: 'tag bucket' }, b))
    : [el('span', { class: 'muted', style: 'font-size:12px' }, 'no bucket')];

  const statusNote = c.runnable
    ? el('span', { class: 'cl-status ok' }, c.connector)
    : (c.tier === 3
        ? el('span', { class: 'cl-status pending' }, 'not yet supported')
        : el('span', { class: 'cl-status pending' }, 'no connector'));

  return el('div', { class: 'company-row' }, [
    el('div', { class: 'cl-main' }, [
      el('div', { class: 'cl-name' }, [ document.createTextNode(c.display_name), statusNote ]),
      el('div', { class: 'cl-buckets' }, bucketTags),
    ]),
    el('div', { class: 'cl-actions' }, [
      el('button', { class: 'btn ghost small', onclick: () => openCompanyBuckets(c) }, 'Buckets'),
      el('button', { class: 'btn danger small', onclick: () => confirmRemoveCompany(c) }, 'Remove'),
    ]),
  ]);
}

/* Edit which buckets ONE company belongs to (the from-the-company direction).
   A checkbox list of every existing bucket, pre-ticked to current membership;
   Save posts the full desired set to /api/company/buckets (which diffs and
   assigns/removes). A company can be in many buckets; ticking here never
   affects other companies. New buckets are still made via "+ New bucket". */
function openCompanyBuckets(c) {
  const allBuckets = manageBuckets().map(b => b.name);
  const current = new Set(c.buckets || []);
  const chosen = new Set(current);

  const picker = el('div', { class: 'check-list' });
  if (!allBuckets.length) {
    picker.appendChild(el('p', { class: 'mc-hint', style: 'margin:0' },
      'No buckets yet — make one with “+ New bucket”, then you can add companies to it here.'));
  } else {
    for (const name of allBuckets) {
      picker.appendChild(el('label', { class: 'check-row' }, [
        el('input', {
          type: 'checkbox', ...(current.has(name) ? { checked: '' } : {}),
          onchange: (e) => { e.target.checked ? chosen.add(name) : chosen.delete(name); },
        }),
        el('span', {}, name),
      ]));
    }
  }

  const saveBtn = el('button', { class: 'btn signal' }, 'Save');
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    try {
      await api.post('/api/company/buckets', { key: c.key, buckets: [...chosen] });
      toast(`Updated buckets for “${c.display_name}”.`);
      veil.remove();
      openManage();
    } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
  });

  const veil = makeModal(`Buckets for “${c.display_name}”`,
    'Tick every bucket this company should be in. A company can be in several at once; changes here never affect other companies.',
    [ el('div', { class: 'field' }, [ el('label', {}, 'Buckets'), picker ]) ],
    saveBtn);
  document.body.appendChild(veil);
}

/* ---- create a bucket: name + checkbox member-picker ------------------- */

function openCreateBucket() {
  const nameInput = el('input', { type: 'text', placeholder: 'e.g. Big Tech', id: 'newBucketName' });
  const chosen = new Set();
  const picker = el('div', { class: 'check-list' });
  for (const c of sortedCompanies()) {
    const row = el('label', { class: 'check-row' }, [
      el('input', { type: 'checkbox', onchange: (e) => {
        e.target.checked ? chosen.add(c.key) : chosen.delete(c.key);
      } }),
      el('span', {}, c.display_name),
    ]);
    picker.appendChild(row);
  }

  const saveBtn = el('button', { class: 'btn signal' }, 'Create bucket');
  saveBtn.addEventListener('click', async () => {
    const name = nameInput.value.trim();
    if (!name) { toast('Give the bucket a name.', true); return; }
    if (manageBuckets().some(b => b.name === name)) {
      toast(`There’s already a bucket called “${name}”.`, true); return;
    }
    saveBtn.disabled = true;
    try {
      // Assign each chosen company to the new bucket. (An empty bucket is fine
      // too — assigning zero companies still "creates" it the moment one joins;
      // to support a truly empty bucket we assign-then-it-exists via members.)
      for (const key of chosen) {
        await api.post('/api/bucket/assign', { key, bucket: name });
      }
      if (!chosen.size) {
        // No members chosen: create the label by assigning+removing a no-op is
        // messy; instead we tell the user buckets appear once they hold a
        // company, OR we can seed it via the sub-bucket roster. Simplest honest
        // path: require at least one member to create.
        toast('Pick at least one company for the new bucket.', true);
        saveBtn.disabled = false;
        return;
      }
      toast(`Created “${name}”.`);
      veil.remove();
      openManage();
    } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
  });

  const veil = makeModal('New bucket',
    'Name the bucket, then tick the companies to put in it. A company can be in several buckets at once — adding it here never removes it from another.',
    [
      el('div', { class: 'field' }, [ el('label', { for: 'newBucketName' }, 'Bucket name'), nameInput ]),
      el('div', { class: 'field' }, [ el('label', {}, 'Companies'), picker ]),
    ],
    saveBtn);
  document.body.appendChild(veil);
  nameInput.focus();
}

/* ---- add existing companies to an existing bucket -------------------- */

/* Add companies to a bucket that already exists (the from-the-bucket direction).
   Lists every company NOT already in this bucket; tick some, Save assigns each
   via /api/bucket/assign. Adding here never removes a company from any other
   bucket (a company can be in several). If every company is already in it, we
   say so rather than showing an empty list. */
function openAddToBucket(bucket) {
  const notIn = sortedCompanies().filter(c => !(c.buckets || []).includes(bucket));
  const chosen = new Set();

  const picker = el('div', { class: 'check-list' });
  if (!notIn.length) {
    picker.appendChild(el('p', { class: 'mc-hint', style: 'margin:0' },
      'Every company is already in this bucket.'));
  } else {
    for (const c of notIn) {
      picker.appendChild(el('label', { class: 'check-row' }, [
        el('input', { type: 'checkbox', onchange: (e) => {
          e.target.checked ? chosen.add(c.key) : chosen.delete(c.key);
        } }),
        el('span', {}, c.display_name),
      ]));
    }
  }

  const saveBtn = el('button', { class: 'btn signal' }, 'Add to bucket');
  saveBtn.addEventListener('click', async () => {
    if (!chosen.size) { toast('Pick at least one company to add.', true); return; }
    saveBtn.disabled = true;
    try {
      for (const key of chosen) {
        await api.post('/api/bucket/assign', { key, bucket });
      }
      toast(`Added ${chosen.size} ${plural(chosen.size, 'company', 'companies')} to “${bucket}”.`);
      veil.remove();
      openManage();
    } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
  });

  const veil = makeModal(`Add companies to “${bucket}”`,
    'Tick the companies to add. This never removes them from any other bucket — a company can be in several at once.',
    [ el('div', { class: 'field' }, [ el('label', {}, 'Companies not yet in this bucket'), picker ]) ],
    saveBtn);
  document.body.appendChild(veil);
}


/* ---- rename a bucket (unique-name enforced server-side) --------------- */

function openRenameBucket(bucket) {
  const input = el('input', { type: 'text', value: bucket, id: 'renameBucket' });
  const saveBtn = el('button', { class: 'btn signal' }, 'Rename');
  saveBtn.addEventListener('click', async () => {
    const next = input.value.trim();
    if (!next) { toast('Give the bucket a name.', true); return; }
    if (next === bucket) { veil.remove(); return; }
    saveBtn.disabled = true;
    try {
      await api.post('/api/bucket/rename', { old: bucket, new: next });
      toast(`Renamed “${bucket}” to “${next}”.`);
      if (MANAGE_FILTER === bucket) MANAGE_FILTER = next;
      veil.remove();
      openManage();
    } catch (e) {
      // The server blocks a rename onto an existing bucket with a plain message.
      toast(e.message, true);
      saveBtn.disabled = false;
    }
  });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveBtn.click(); } });

  const veil = makeModal(`Rename “${bucket}”`,
    'Buckets each have their own name — if you pick a name that’s already taken, JobWatch will say so rather than merging them.',
    [ el('div', { class: 'field' }, [ el('label', { for: 'renameBucket' }, 'New name'), input ]) ],
    saveBtn);
  document.body.appendChild(veil);
  input.focus(); input.select();
}

/* ---- confirmations: delete bucket / remove company ------------------- */

function confirmDeleteBucket(bucket, count) {
  const veil = modal(`Delete the “${bucket}” bucket?`,
    `This removes the “${bucket}” label from ${count} ${plural(count, 'company', 'companies')}. The ${plural(count, 'company stays', 'companies stay')} — only the grouping goes away. Nothing about their tracked history changes.`,
    [],
    async () => {
      try {
        const r = await api.post('/api/bucket/delete', { bucket });
        toast(`Removed the “${bucket}” label. ${r.companies_kept} ${plural(r.companies_kept, 'company', 'companies')} kept.`);
        if (MANAGE_FILTER === bucket) MANAGE_FILTER = null;
        openManage();
        return true;
      } catch (e) { toast(e.message, true); return false; }
    },
    'Delete bucket');
  const btn = veil.querySelector('.btn.signal');
  if (btn) { btn.classList.remove('signal'); btn.classList.add('danger'); }
  document.body.appendChild(veil);
}

function confirmRemoveCompany(company) {
  const veil = modal(`Remove ${company.display_name}?`,
    'This stops tracking the company and removes it from every bucket it’s in. Its past snapshots stay on disk (history is never deleted), but it won’t appear in checks anymore. You can always add it back later.',
    [],
    async () => {
      try {
        await api.post('/api/company/remove', { key: company.key });
        toast(`Removed ${company.display_name}.`);
        openManage();
        return true;
      } catch (e) { toast(e.message, true); return false; }
    },
    'Remove company');
  const btn = veil.querySelector('.btn.signal');
  if (btn) { btn.classList.remove('signal'); btn.classList.add('danger'); }
  document.body.appendChild(veil);
}

/* ---- add a company: paste URL → detect → tier-route ------------------ */

function openAddCompany() {
  let detection = null;
  const urlInput = el('input', { type: 'text', placeholder: 'https://… careers page URL', id: 'addUrl' });
  const result = el('div', { class: 'detect-result', id: 'detectResult' });
  const nameInput = el('input', { type: 'text', placeholder: 'Company name', id: 'addName' });
  const keyInput = el('input', { type: 'text', placeholder: 'short-id', id: 'addKey' });
  const bucketInput = el('input', { type: 'text', placeholder: 'e.g. Big Tech, London (comma-separated)', id: 'addBuckets' });
  const detailFields = el('div', { class: 'hidden', id: 'addDetails' }, [
    el('div', { class: 'field' }, [ el('label', {}, 'Name'), nameInput ]),
    el('div', { class: 'field' }, [ el('label', {}, 'Short id (used internally — lowercase, no spaces)'), keyInput ]),
    el('div', { class: 'field' }, [ el('label', {}, 'Buckets (optional)'), bucketInput ]),
  ]);

  const detectBtn = el('button', { class: 'btn small' }, 'Detect');
  const saveBtn = el('button', { class: 'btn signal', disabled: '' }, 'Add company');

  async function runDetect() {
    const url = urlInput.value.trim();
    if (!url) { toast('Paste a careers URL first.', true); return; }
    detectBtn.disabled = true; detectBtn.textContent = 'Checking…';
    try {
      const r = await api.get('/api/detect?url=' + encodeURIComponent(url));
      detection = r.detection;
      showDetection(detection, url);
    } catch (e) {
      toast(e.message, true);
    } finally {
      detectBtn.disabled = false; detectBtn.textContent = 'Detect';
    }
  }

  function showDetection(d, url) {
    result.replaceChildren();
    // Tier 1 (provider, connector exists) OR Tier 2 (preset) → addable here.
    const addable = d.provider && d.tier !== 3;
    if (addable) {
      result.appendChild(el('div', { class: 'detect-msg good' }, d.message));
      nameInput.value = d.display_guess || '';
      keyInput.value = d.suggested_key || '';
      detailFields.classList.remove('hidden');
      saveBtn.disabled = false;
      saveBtn._detection = d;
    } else {
      // Tier 3 → route to the honest help page instead of a dead-end.
      detailFields.classList.add('hidden');
      saveBtn.disabled = true;
      saveBtn._detection = null;
      result.appendChild(el('div', { class: 'detect-msg warn' }, [
        el('div', {}, d.message),
        el('button', { class: 'btn small', style: 'margin-top:10px',
          onclick: () => { veil.remove(); openTier3Help(d, url); } },
          'What can I do? →'),
      ]));
    }
  }

  detectBtn.addEventListener('click', runDetect);
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runDetect(); } });

  saveBtn.addEventListener('click', async () => {
    const d = saveBtn._detection;
    if (!d) return;
    const name = nameInput.value.trim();
    const key = keyInput.value.trim().toLowerCase();
    if (!name) { toast('Give the company a name.', true); return; }
    if (!key) { toast('Give the company a short id.', true); return; }
    const buckets = bucketInput.value.split(',').map(s => s.trim()).filter(Boolean);
    saveBtn.disabled = true;
    try {
      await api.post('/api/company/add', {
        key, display_name: name, connector: d.provider,
        config: d.config || {}, buckets, tier: d.tier || 1,
      });
      toast(`Added ${name}.`);
      veil.remove();
      openManage();
    } catch (e) {
      toast(e.message, true);
      saveBtn.disabled = false;
    }
  });

  const veil = makeModal('Add a company',
    'Paste the company’s careers-page URL. JobWatch figures out which job board it uses — no codes or jargon. If it’s a board we don’t support yet, it’ll tell you plainly and show you what to do.',
    [
      el('div', { class: 'field' }, [
        el('label', { for: 'addUrl' }, 'Careers page URL'),
        el('div', { class: 'url-row' }, [ urlInput, detectBtn ]),
      ]),
      result,
      detailFields,
    ],
    saveBtn);
  document.body.appendChild(veil);
  urlInput.focus();
}

/* ---- Tier-3 help (in Add-a-Company) ---------------------------------- */

/* A URL the user pasted that turned out to be custom — stashed so the Guide can
   pre-fill its careers-URL field when we route there. Cleared after use. */
let GUIDE_PREFILL_URL = null;

function openTier3Help(detection, url) {
  // Two flavours: (a) a recognised provider with no connector built yet — which
  // might still just be a marketing wrapper, so the reclassify tip is worth a
  // try; (b) unidentified / genuinely custom — which needs the inspection guide.
  const recognised = !!detection.provider;
  const view = $('#view');
  const children = [
    el('button', { class: 'backlink', onclick: () => { go('companies'); setTimeout(openAddCompany, 50); } }, '← Back to adding a company'),
    el('div', { class: 'sec-head', style: 'margin-bottom:10px' }, [ el('h2', {}, 'This one needs a connector built') ]),
  ];

  children.push(el('div', { class: 'help-card' }, [
    el('p', { class: 'help-lead' }, detection.message),
  ]));

  if (recognised) {
    children.push(el('div', { class: 'help-block' }, [
      el('h3', {}, 'What’s happening'),
      el('p', {}, `JobWatch recognises this as a “${detection.provider}” board but doesn’t have a working reader for it yet. Before treating it as fully custom, it’s worth one quick check: the page you pasted might be a wrapper over a board JobWatch already reads.`),
      el('h3', {}, 'Try this first'),
      el('ul', {}, [
        el('li', {}, 'Open the careers page, click into an actual job, and copy the URL it lands on. If that URL looks like Greenhouse / Lever / Ashby / Workday and the like, paste that one instead — it’ll just work.'),
        el('li', {}, 'If it really is this custom board, it needs a reader built. The guide below walks you through capturing what an agent needs to build it.'),
      ]),
    ]));
  } else {
    children.push(el('div', { class: 'help-block' }, [
      el('h3', {}, 'What’s happening'),
      el('p', {}, 'JobWatch couldn’t identify a supported job board from that link. Either the page hides a standard board underneath, or it’s a fully custom careers site.'),
      el('h3', {}, 'Two things to try'),
      el('ul', {}, [
        el('li', {}, 'Click into a single job and copy the URL it lands on — if it’s a standard board (Greenhouse, Lever, Ashby…), paste that and you’re done.'),
        el('li', {}, 'If it’s genuinely custom, it needs a reader built — a short collaboration. The guide below walks you through capturing what’s needed.'),
      ]),
    ]));
  }

  if (url) {
    children.push(el('p', { class: 'help-url' }, [
      el('span', { class: 'muted' }, 'You pasted: '),
      el('a', { href: url, target: '_blank', rel: 'noopener' }, url),
    ]));
  }

  children.push(el('div', { class: 'btn-row', style: 'margin-top:20px' }, [
    el('button', { class: 'btn signal', onclick: () => { GUIDE_PREFILL_URL = url || null; go('setup'); } }, 'Open the custom-board guide →'),
    el('button', { class: 'btn ghost', onclick: () => { go('companies'); setTimeout(openAddCompany, 50); } }, 'Try another URL'),
  ]));

  view.replaceChildren(...children);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* =======================================================================
   SUB-BUCKET MODE (Phase I) — same-page reorganise.
   Left: name the sub-buckets. Right: this bucket's companies, dragged into a
   sub-bucket. Anything left unplaced lands in Other/Misc. Save → confirm.
   ======================================================================= */

const OTHER_MISC = 'Other/Misc';

async function enterSubMode(bucket) {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Loading sub-buckets…'));
  let layout;
  try {
    const r = await api.get('/api/sub-buckets?bucket=' + encodeURIComponent(bucket));
    layout = r.layout;
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t open sub-buckets.', e.message));
    return;
  }
  // Build working state. Names exclude Other/Misc (it's implicit, always last).
  const names = (layout.sub_buckets || []).filter(n => n !== OTHER_MISC);
  const assign = {};
  for (const c of layout.companies || []) assign[c.key] = c.sub_bucket || OTHER_MISC;
  SUBMODE = { bucket, names, assign, companies: (layout.companies || []) };
  drawSubMode(true);
}

function exitSubMode() { SUBMODE = null; openManage(); }

function subColumns() {
  // The ordered list of drop columns = named sub-buckets + Other/Misc last.
  return [...SUBMODE.names, OTHER_MISC];
}

function drawSubMode(scrollTop = false) {
  const view = $('#view');
  const S = SUBMODE;
  const children = [
    el('button', { class: 'backlink', onclick: exitSubMode }, '← Back to companies & buckets'),
    el('div', { class: 'mc-head' }, [
      el('div', { class: 'sec-head', style: 'margin:0' }, [
        el('h2', {}, `Sub-buckets for “${S.bucket}”`),
        el('span', { class: 'hint' }, 'Drag a company into a sub-bucket — anything left over goes to Other/Misc'),
      ]),
      el('button', { class: 'btn signal', onclick: confirmSaveSubMode }, 'Save'),
    ]),
  ];

  const board = el('div', { class: 'mc-board' }, [ subNamesColumn(), subDropColumn() ]);
  children.push(board);
  view.replaceChildren(...children);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* LEFT — name / add / remove sub-buckets */
function subNamesColumn() {
  const col = el('div', { class: 'mc-col mc-buckets' });
  col.appendChild(el('div', { class: 'mc-col-head' }, [ el('h3', {}, 'Sub-buckets') ]));

  for (const name of SUBMODE.names) {
    const row = el('div', { class: 'mc-bucket-row' });
    row.appendChild(el('span', { class: 'mcb-name', style: 'padding:6px 2px' }, name));
    row.appendChild(el('div', { class: 'mcb-actions' }, [
      el('button', { class: 'icon-btn', title: 'Rename sub-bucket',
        onclick: () => renameSub(name) }, penGlyph()),
      el('button', { class: 'icon-btn danger', title: 'Remove sub-bucket (its companies move to Other/Misc)',
        onclick: () => removeSub(name) }, '×'),
    ]));
    col.appendChild(row);
  }

  // Other/Misc shown as a fixed, non-removable entry for clarity.
  col.appendChild(el('div', { class: 'mc-bucket-row muted-row' }, [
    el('span', { class: 'mcb-name', style: 'padding:6px 2px' }, OTHER_MISC),
    el('span', { class: 'mcb-fixed' }, 'always here'),
  ]));

  // add a new sub-bucket inline
  const input = el('input', { type: 'text', placeholder: 'New sub-bucket name', id: 'newSub' });
  const addBtn = el('button', { class: 'btn small' }, 'Add');
  const addNew = () => {
    const n = input.value.trim();
    if (!n) return;
    if (n === OTHER_MISC) { toast('Other/Misc always exists.', true); return; }
    if (SUBMODE.names.includes(n)) { toast('That sub-bucket already exists.', true); return; }
    SUBMODE.names.push(n);
    input.value = '';
    drawSubMode();
  };
  addBtn.addEventListener('click', addNew);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addNew(); } });
  col.appendChild(el('div', { class: 'field', style: 'margin:16px 0 0' }, [
    el('div', { class: 'url-row' }, [ input, addBtn ]),
  ]));
  return col;
}

function renameSub(name) {
  const input = el('input', { type: 'text', value: name, id: 'subRename' });
  const saveBtn = el('button', { class: 'btn signal' }, 'Rename');
  saveBtn.addEventListener('click', () => {
    const next = input.value.trim();
    if (!next) { toast('Give it a name.', true); return; }
    if (next === name) { veil.remove(); return; }
    if (next === OTHER_MISC || SUBMODE.names.includes(next)) {
      toast('That name’s taken.', true); return;
    }
    SUBMODE.names = SUBMODE.names.map(n => n === name ? next : n);
    for (const k in SUBMODE.assign) if (SUBMODE.assign[k] === name) SUBMODE.assign[k] = next;
    veil.remove();
    drawSubMode();
  });
  const veil = makeModal(`Rename “${name}”`, 'This only changes the sub-bucket’s name within this bucket.',
    [ el('div', { class: 'field' }, [ el('label', {}, 'New name'), input ]) ], saveBtn);
  document.body.appendChild(veil);
  input.focus(); input.select();
}

function removeSub(name) {
  // Companies in it fall back to Other/Misc; no confirm needed (non-destructive).
  SUBMODE.names = SUBMODE.names.filter(n => n !== name);
  for (const k in SUBMODE.assign) if (SUBMODE.assign[k] === name) SUBMODE.assign[k] = OTHER_MISC;
  drawSubMode();
}

/* RIGHT — the drop columns with draggable company cards */
function subDropColumn() {
  const col = el('div', { class: 'mc-col mc-companies' });
  const cols = subColumns();
  const byName = {};
  for (const n of cols) byName[n] = [];
  for (const c of SUBMODE.companies) {
    const place = SUBMODE.assign[c.key] || OTHER_MISC;
    (byName[place] || byName[OTHER_MISC]).push(c);
  }

  const grid = el('div', { class: 'sub-grid' });
  for (const name of cols) {
    const zone = el('div', { class: 'sub-zone', 'data-sub': name });
    zone.appendChild(el('div', { class: 'sub-zone-head' }, [
      el('span', { class: 'sz-name' }, name),
      el('span', { class: 'sz-count' }, `${byName[name].length}`),
    ]));
    const drop = el('div', { class: 'sub-drop' });
    // drag-over affordances
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (e) => {
      e.preventDefault();
      drop.classList.remove('over');
      const key = e.dataTransfer.getData('text/plain');
      if (key) { SUBMODE.assign[key] = name; drawSubMode(); }
    });

    if (!byName[name].length) {
      drop.appendChild(el('div', { class: 'sub-empty' }, 'Drop companies here'));
    } else {
      for (const c of byName[name]) drop.appendChild(subCard(c, cols, name));
    }
    zone.appendChild(drop);
    grid.appendChild(zone);
  }
  col.appendChild(grid);
  return col;
}

function subCard(c, cols, current) {
  const card = el('div', { class: 'sub-card', draggable: 'true', 'data-key': c.key }, [
    el('span', { class: 'sc-name', title: c.display_name }, c.display_name),
    // click fallback: a "move to…" select so drag isn't the only way
    moveToSelect(c.key, cols, current),
  ]);
  card.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', c.key);
    e.dataTransfer.effectAllowed = 'move';
    card.classList.add('dragging');
  });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
  return card;
}

/* The click/keyboard fallback the spec requires under the drag. */
function moveToSelect(key, cols, current) {
  const sel = el('select', { class: 'move-to', title: 'Move to…',
    onchange: (e) => { SUBMODE.assign[key] = e.target.value; drawSubMode(); } });
  for (const name of cols) {
    sel.appendChild(el('option', { value: name, ...(name === current ? { selected: '' } : {}) }, name));
  }
  // stop a drag starting from the select itself
  sel.addEventListener('mousedown', (e) => e.stopPropagation());
  return sel;
}

function confirmSaveSubMode() {
  const S = SUBMODE;
  // Count what lands where for a plain confirmation line.
  const placed = Object.values(S.assign).filter(v => v && v !== OTHER_MISC).length;
  const misc = S.companies.length - placed;
  const veil = modal('Save sub-buckets?',
    `${placed} ${plural(placed, 'company goes', 'companies go')} into your named sub-buckets` +
    (misc ? `, and ${misc} into Other/Misc` : '') +
    `. You can reorganise any time.`,
    [],
    async () => {
      try {
        await api.post('/api/sub-buckets/save', {
          bucket: S.bucket, names: S.names, assignments: S.assign,
        });
        toast(`Saved sub-buckets for “${S.bucket}”.`);
        SUBMODE = null;
        openManage();
        return true;
      } catch (e) { toast(e.message, true); return false; }
    },
    'Save');
  document.body.appendChild(veil);
}

/* ---- small shared modal builder + glyphs ----------------------------- */

function makeModal(title, lead, fields, primaryBtn) {
  const veil = el('div', { class: 'modal-veil' });
  veil.addEventListener('click', (e) => { if (e.target === veil) veil.remove(); });
  veil.appendChild(el('div', { class: 'modal' }, [
    el('h2', {}, title),
    lead ? el('p', {}, lead) : null,
    ...fields,
    el('div', { class: 'modal-actions' }, [
      el('button', { class: 'btn ghost', onclick: () => veil.remove() }, 'Cancel'),
      primaryBtn,
    ]),
  ]));
  return veil;
}

function subGlyph() {
  return svgEl('0 0 16 16', [
    { d: 'M2 3.5h12', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.4', 'stroke-linecap': 'round' },
    { d: 'M4 7.5h10', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.4', 'stroke-linecap': 'round' },
    { d: 'M4 11.5h10', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.4', 'stroke-linecap': 'round' },
  ], 'glyph');
}
function penGlyph() {
  return svgEl('0 0 16 16', [
    { d: 'M11 2.5l2.5 2.5L6 12.5l-3 .8.8-3L11 2.5z', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.3', 'stroke-linejoin': 'round' },
  ], 'glyph');
}
function plusGlyph() {
  return svgEl('0 0 16 16', [
    { d: 'M8 3v10', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5', 'stroke-linecap': 'round' },
    { d: 'M3 8h10', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5', 'stroke-linecap': 'round' },
  ], 'glyph');
}

/* =======================================================================
   HISTORY (Phase J) — browse past phases. A real, banner-reachable route.
   Two views on one route: a list of every phase (newest first), and a drill-in
   showing one phase's companies with their last-check date and role count. Was
   the dormant-only "quiet archive"; now a top-level page in the v2 shell.
   ======================================================================= */

let HISTORY_PHASE = null;   // a phase id when drilled into one, else null (the list)
let HISTORY_CONFIRM_DELETE = false;  // inline "confirm delete" state on the phase
                                    // detail view (Post-Phase-O). Transient: reset
                                    // whenever we (re)enter a phase or leave History.
let HISTORY_ROW_CONFIRM = null;      // phase id whose LIST row is showing its inline
                                    // delete-confirm strip, else null. Only one at
                                    // a time; reset on (re)entering the list.

/* Route entry point. If we're drilled into a phase, show that; else the list. */
async function openHistory() {
  if (HISTORY_PHASE) { drawHistoryPhase(HISTORY_PHASE, true); }
  else { drawHistoryList(true); }
}

/* Kept for back-compat: older callers (the dormant reading-spine's "Browse past
   phases") used openArchive(). Route through History so the hash stays in sync. */
function openArchive() { HISTORY_PHASE = null; go('history'); }

/* 2a — the phase list. */
async function drawHistoryList(scrollTop = false) {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Opening your history…'));
  let data;
  try {
    data = await api.get('/api/archive');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t open your history.', e.message));
    return;
  }
  const phasesList = (data.phases || []).slice().reverse(); // newest first
  const children = [
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
    el('div', { class: 'sec-head' }, [ el('h2', {}, 'History') ]),
    el('p', { class: 'lead' }, 'Every phase you’ve been through, newest first. Open one to see which companies you tracked in it and how many roles each was showing.'),
  ];

  if (!phasesList.length) {
    children.push(el('div', { class: 'empty' }, [
      el('h3', {}, 'Nothing here yet'),
      el('p', {}, 'Once you’ve run a hunt or two, each phase shows up here as a record you can look back on.'),
    ]));
    $('#view').replaceChildren(...children);
    if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  const list = el('div', { class: 'company-list' });
  for (const p of phasesList) {
    const span = p.ended_on ? `${p.started_on} → ${p.ended_on}` : `${p.started_on} → now`;
    const dataNote = p.companies_with_data
      ? `${p.companies_with_data} ${plural(p.companies_with_data, 'company', 'companies')} recorded` +
        (p.last_check ? ` · last ${p.last_check}` : '')
      : 'no checks recorded';

    // Row = a clickable main area (opens the phase) + a trash icon. The whole
    // row can't be one <button> any more (a button can't nest a button), so the
    // main area is the button and the trash sits beside it. Clicking trash opens
    // an INLINE confirm strip on this row — you never leave the list (Post-O).
    const row = el('div', { class: 'company-row history-row' });
    const mainBtn = el('button', { class: 'history-row-main', onclick: () => openHistoryPhase(p.id) }, [
      el('div', { class: 'cl-main' }, [
        el('div', { class: 'cl-name' }, [
          document.createTextNode(p.name),
          p.is_current
            ? el('span', { class: 'cl-status ok' }, 'current')
            : el('span', { class: 'cl-status pending' }, p.type || ''),
        ]),
        el('div', { class: 'cl-buckets' }, [
          el('span', { class: 'muted', style: 'font-size:12px' }, `${span}  ·  ${dataNote}`),
        ]),
      ]),
      el('span', { class: 'history-chev', 'aria-hidden': 'true' }, '›'),
    ]);

    // Render the row body in either resting (trash) or confirm state, in place.
    const renderRowBody = () => {
      if (HISTORY_ROW_CONFIRM === p.id) {
        row.classList.add('confirming');
        row.replaceChildren(
          el('div', { class: 'row-confirm' }, [
            el('span', { class: 'row-confirm-text' },
              p.is_current
                ? `Delete current phase “${p.name}”? Ends the hunt and erases its data. Can’t be undone.`
                : `Delete “${p.name}” and all its data? Can’t be undone.`),
            el('div', { class: 'row-confirm-actions' }, [
              el('button', { class: 'btn ghost small',
                onclick: () => { HISTORY_ROW_CONFIRM = null; renderRowBody(); } }, 'Cancel'),
              el('button', { class: 'btn danger small',
                onclick: () => deletePhaseFromList(p) }, 'Delete'),
            ]),
          ])
        );
      } else {
        row.classList.remove('confirming');
        row.replaceChildren(
          mainBtn,
          el('button', {
            class: 'icon-btn danger history-trash',
            title: `Delete “${p.name}”`,
            'aria-label': `Delete phase ${p.name}`,
            onclick: () => { HISTORY_ROW_CONFIRM = p.id; renderRowBody(); },
          }, trashGlyph()),
        );
      }
    };
    renderRowBody();
    list.appendChild(row);
  }
  children.push(list);
  $('#view').replaceChildren(...children);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* Delete a phase from the LIST view's inline confirm. Same wipe+remove as the
   detail-view delete; afterwards we re-render the list in place (and refresh
   home/banner/theme if it was the current phase, since the app is now dormant). */
async function deletePhaseFromList(phase) {
  try {
    const res = await api.post('/api/phase/delete', { id: phase.id });
    const w = (res && res.wiped) || {};
    const bits = [];
    if (w.snapshots) bits.push(`${w.snapshots} ${plural(w.snapshots, 'check', 'checks')}`);
    if (w.applications) bits.push(`${w.applications} ${plural(w.applications, 'application', 'applications')}`);
    if (w.saved) bits.push(`${w.saved} saved`);
    const detail = bits.length ? ` (cleared ${bits.join(', ')})` : '';
    toast(`Deleted “${phase.name}”${detail}.`);

    HISTORY_ROW_CONFIRM = null;
    if (res && res.was_current) {
      try { HOME = await api.get('/api/home'); } catch (_) {}
      applyTheme();
      renderBanner();
    }
    drawHistoryList();   // re-fetch + redraw; the phase is gone now
  } catch (e) {
    toast(e.message, true);
  }
}

/* Open one phase's detail. Sets state then renders; the route stays 'history'
   so a refresh keeps you in History (it returns to the list, which is fine —
   the drill-in is a within-page state, not its own URL). */
function openHistoryPhase(phaseId) {
  HISTORY_PHASE = phaseId;
  HISTORY_CONFIRM_DELETE = false;
  HISTORY_ROW_CONFIRM = null;
  drawHistoryPhase(phaseId, true);
}

function backToHistoryList() {
  HISTORY_PHASE = null;
  HISTORY_CONFIRM_DELETE = false;
  HISTORY_ROW_CONFIRM = null;
  drawHistoryList(true);
}

/* 2b — one phase's companies, each with last-check date + role count. */
async function drawHistoryPhase(phaseId, scrollTop = false) {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Opening that phase…'));
  let data;
  try {
    data = await api.get('/api/archive/phase?id=' + encodeURIComponent(phaseId));
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t open that phase.', e.message));
    return;
  }
  const phase = data.phase;
  if (!phase) {
    // Phase vanished (shouldn't normally happen) — fall back to the list.
    HISTORY_PHASE = null;
    drawHistoryList(true);
    return;
  }

  const span = phase.ended_on ? `${phase.started_on} → ${phase.ended_on}` : `${phase.started_on} → now`;
  const companiesList = data.companies || [];

  const children = [
    el('button', { class: 'backlink', onclick: backToHistoryList }, '← Back to history'),
    el('div', { class: 'sec-head' }, [
      el('h2', {}, phase.name),
      phase.is_current
        ? el('span', { class: 'cl-status ok' }, 'current')
        : el('span', { class: 'cl-status pending' }, phase.type || ''),
      el('button', {
        class: 'icon-btn danger sec-head-trash',
        title: `Delete “${phase.name}”`,
        'aria-label': `Delete phase ${phase.name}`,
        onclick: () => { HISTORY_CONFIRM_DELETE = true; drawHistoryPhase(phase.id); },
      }, trashGlyph()),
    ]),
    el('p', { class: 'lead' }, `${phase.type === 'casual' ? 'Casual watch' : 'Active hunt'}  ·  ${span}`),
  ];

  if (!companiesList.length) {
    children.push(el('div', { class: 'empty' }, [
      el('h3', {}, 'No checks recorded in this phase'),
      el('p', {}, 'This phase has no snapshots yet — nothing was checked while it was running.'),
    ]));
    children.push(phaseDeleteSection(phase));
    $('#view').replaceChildren(...children);
    if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  const list = el('div', { class: 'company-list' });
  for (const c of companiesList) {
    list.appendChild(el('div', { class: 'company-row' }, [
      el('div', { class: 'cl-main' }, [
        el('div', { class: 'cl-name' }, c.display_name || c.key),
        el('div', { class: 'cl-buckets' }, [
          el('span', { class: 'muted', style: 'font-size:12px' },
            c.last_check ? `last checked ${c.last_check}` : 'checked'),
        ]),
      ]),
      el('span', { class: 'history-count' }, [
        el('b', {}, String(c.role_count)),
        document.createTextNode(' ' + plural(c.role_count, 'role', 'roles')),
      ]),
    ]));
  }
  children.push(list);
  children.push(phaseDeleteSection(phase));
  $('#view').replaceChildren(...children);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* The "Delete this phase" affordance at the foot of a phase's History detail
   (Post-Phase-O — clearing out test phases). Two states, swapped IN PLACE with
   no popup (the user asked for an inline confirm, not a modal):
     • resting  → a single danger "Delete this phase" button.
     • confirm  → a warning line + "Confirm delete" (danger) and "Cancel".
   Deleting is a TRUE FULL WIPE (snapshots + trends + applications + saved). The
   warning is stronger when the phase is the CURRENT one, because deleting it
   ends the live hunt and drops the app to dormant. State lives in
   HISTORY_CONFIRM_DELETE; toggling it re-renders this view. */
function phaseDeleteSection(phase) {
  const wrap = el('div', { class: 'phase-delete' });

  if (!HISTORY_CONFIRM_DELETE) {
    wrap.appendChild(el('button', {
      class: 'btn danger small',
      onclick: () => { HISTORY_CONFIRM_DELETE = true; drawHistoryPhase(phase.id); },
    }, 'Delete this phase'));
    return wrap;
  }

  // Confirm state.
  const isCurrent = !!phase.is_current;
  const warning = isCurrent
    ? el('p', { class: 'phase-delete-warn strong' }, [
        el('b', {}, 'This is your current phase. '),
        document.createTextNode(
          'Deleting it ends the live hunt and drops the app to its resting state, ' +
          'and permanently removes this phase and everything recorded in it — ' +
          'its checks, trends, applications and saved roles. This can’t be undone.'),
      ])
    : el('p', { class: 'phase-delete-warn' },
        'This permanently removes this phase and everything recorded in it — its ' +
        'checks, trends, applications and saved roles. This can’t be undone. ' +
        'Other phases are not affected.');

  wrap.appendChild(warning);
  wrap.appendChild(el('div', { class: 'phase-delete-actions' }, [
    el('button', {
      class: 'btn ghost small',
      onclick: () => { HISTORY_CONFIRM_DELETE = false; drawHistoryPhase(phase.id); },
    }, 'Cancel'),
    el('button', {
      class: 'btn danger small',
      onclick: () => deletePhase(phase),
    }, isCurrent ? 'Delete current phase' : 'Confirm delete'),
  ]));
  // The confirm panel sits at the foot of the detail; if the user opened it from
  // the header trash they're scrolled to the top, so bring it into view.
  const raf = (typeof requestAnimationFrame === 'function')
    ? requestAnimationFrame : (fn) => setTimeout(fn, 0);
  raf(() => {
    try { wrap.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (_) {}
  });
  return wrap;
}

/* Fire the delete: POST the wipe+remove, then leave. If the deleted phase was
   the current one, the app is now dormant — refresh home so the banner, theme
   (data-theme → dormant) and everything re-skin. Then return to the History
   list, where the phase is now gone. */
async function deletePhase(phase) {
  try {
    const res = await api.post('/api/phase/delete', { id: phase.id });
    const w = (res && res.wiped) || {};
    const bits = [];
    if (w.snapshots) bits.push(`${w.snapshots} ${plural(w.snapshots, 'check', 'checks')}`);
    if (w.applications) bits.push(`${w.applications} ${plural(w.applications, 'application', 'applications')}`);
    if (w.saved) bits.push(`${w.saved} saved`);
    const detail = bits.length ? ` (cleared ${bits.join(', ')})` : '';
    toast(`Deleted “${phase.name}”${detail}.`);

    HISTORY_CONFIRM_DELETE = false;
    HISTORY_PHASE = null;

    // If we deleted the current phase, the app is dormant now — refresh home
    // state + theme AND re-render the banner before showing the history list.
    // (Re-skinning alone left the top-left switcher label stale on the old type;
    // renderBanner() redraws the switcher from the refreshed HOME.phase_state.)
    if (res && res.was_current) {
      try { HOME = await api.get('/api/home'); } catch (_) {}
      applyTheme();
      renderBanner();
    }
    drawHistoryList(true);
  } catch (e) {
    toast(e.message, true);
  }
}

/* =======================================================================
   TRENDS — how hiring moves over time (Phase F)
   The counts have been recorded every run since Phase B; this screen just
   charts what's there. A hand-rolled SVG line chart, no library, no build.
   You CLICK which lines to show. Lines are banded by phase: the chart never
   draws across the gap between two phases (the golden Phase-B rule).
   ======================================================================= */

/* Phase N — the Trends screen is now TWO sub-tabs:
     • Company hiring  — how the market moves (open / added / removed roles over
       time, by team or location, for companies you tick). Reads /api/trends.
     • My applications — how YOUR hunt is going (a funnel + rates + a weekly
       line). Reads /api/tracker-trends. Phase-scoped (current by default).
   Both honour the locked v1 rules: never one line across a phase gap, degrade
   gracefully on sparse data, look intentional at n=1. */

let TRENDS_TAB = 'company';   // 'company' | 'applications'

// ---- Company-hiring sub-tab state ----
let TRENDS = null;            // last /api/trends payload
let TREND_AXIS = 'department'; // 'department' | 'location' (single-company split)
let TREND_METRIC = 'open';     // 'open' | 'added' | 'removed'
let TREND_PHASE = '';          // '' = all phases (the optional zoom)
let TREND_BREAKDOWN = false;    // false = one combined line; true = per-company/per-split
let TREND_HILITE = null;        // series label currently hovered (dims the rest)
let TREND_PICKER = null;       // the bucket→sub-bucket→company tree (/api/trends/companies)
let TREND_COMPANIES = null;    // Set of company keys committed to the chart (null = all)
let TREND_GRAIN = 'week';      // 'day' | 'week' — EXPLICIT now. Was inferred from
                               // the number of check dates (>12 => weekly), which
                               // silently changed the chart under the user.
let TREND_RANGE = null;        // {from,to} date window; null = phase start → today
let TREND_DRAFT = null;        // Set being edited inside the open dropdown (before Apply)
let TREND_DD_OPEN = false;      // is the company dropdown open
let TREND_DD_SEARCH = '';       // live search text inside the dropdown

// Page-level location filter (moved above the sub-tabs — drives Company hiring).
let TREND_LOCATIONS = null;    // array of location filters (null = use saved Jobs default)
let TREND_LOC_SAVED = null;    // the saved Jobs locations, for the "reset" hint

// ---- My-applications sub-tab state ----
let TRACKER_TRENDS = null;     // last /api/tracker-trends payload
let TRACKER_PHASE = '';        // '' = current phase
let TRACKER_BUCKET = '';       // '' = all buckets
let TRACKER_SUB = '';          // '' = all sub-buckets

async function openTrends() {
  // Pick up the user's saved Jobs location filter once, to seed the page-level
  // location default (the locked "pre-fill from saved Jobs" decision).
  if (TREND_LOCATIONS === null) {
    try {
      const r = await api.get('/api/interests');
      const locs = (r && r.interests && r.interests.locations_allowed) || [];
      TREND_LOCATIONS = locs.slice();
      TREND_LOC_SAVED = locs.slice();
    } catch (e) { TREND_LOCATIONS = []; TREND_LOC_SAVED = []; }
  }
  drawTrendsShell();
  if (TRENDS_TAB === 'company') await loadCompanyTrends();
  else await loadTrackerTrends();
}

/* The shell: a page-level location filter, then a sub-tab strip, then the active
   tab's body mounts into #trendsBody. */
function drawTrendsShell() {
  const view = $('#view');
  const isCasual = HOME && HOME.phase && HOME.phase.type === 'casual';
  const tabs = el('div', { class: 'jobs-tabs' }, [
    el('button', {
      class: 'jobs-tab' + (TRENDS_TAB === 'company' ? ' on' : ''),
      onclick: () => { if (TRENDS_TAB !== 'company') { TRENDS_TAB = 'company'; openTrends(); } },
    }, 'Company hiring'),
    el('button', {
      class: 'jobs-tab' + (TRENDS_TAB === 'applications' ? ' on' : ''),
      onclick: () => { if (TRENDS_TAB !== 'applications') { TRENDS_TAB = 'applications'; openTrends(); } },
    }, 'My applications'),
  ]);
  view.replaceChildren(
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
    el('div', { class: 'sec-head', style: 'margin:0 0 10px' }, [
      el('h2', {}, isCasual ? 'Market movements' : 'Trends'),
    ]),
    // Page-level location filter — applies across the Trends page.
    buildLocationFilter(),
    tabs,
    el('div', { id: 'trendsBody' }, el('div', { class: 'loading' }, 'Reading the history…')),
  );
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function mountTrendsBody(node) {
  const host = $('#trendsBody');
  if (host) host.replaceChildren(node);
}

/* =======================================================================
   COMPANY-HIRING sub-tab
   ======================================================================= */

async function loadCompanyTrends() {
  // Make sure we have the picker tree (buckets → sub-buckets → companies).
  if (TREND_PICKER === null) {
    try {
      TREND_PICKER = await api.get('/api/trends/companies');
    } catch (e) {
      mountTrendsBody(errorBox('Couldn’t load your companies.', e.message));
      return;
    }
  }
  // Default selection: ALL companies (null = everything, summed into one total).
  // TREND_COMPANIES stays null to mean "all"; a committed Set means a subset.

  const selected = committedCompanyKeys();       // resolved list (all, or the subset)
  const single = selected.length === 1;

  const qs = new URLSearchParams();
  // Axis: when a single company is shown, honour the by-team/by-location split;
  // otherwise chart BY COMPANY so the breakdown draws one line per company.
  qs.set('axis', single ? TREND_AXIS : 'company');
  qs.set('metric', TREND_METRIC);
  if (TREND_PHASE) qs.set('phase', TREND_PHASE);
  if (TREND_COMPANIES && TREND_COMPANIES.size)
    qs.set('companies', [...TREND_COMPANIES].join(','));
  const locs = TREND_LOCATIONS || [];
  if (locs.length) qs.set('locations', locs.join(','));

  try {
    TRENDS = await api.get('/api/trends?' + qs.toString());
  } catch (e) {
    mountTrendsBody(errorBox('Couldn’t read your trends.', e.message));
    return;
  }
  drawCompanyTrends();
}

/* Resolve the committed selection to a flat list of company keys. null = all. */
function committedCompanyKeys() {
  if (TREND_COMPANIES && TREND_COMPANIES.size) return [...TREND_COMPANIES];
  return (TREND_PICKER && TREND_PICKER.all_companies || []).map(c => c.key);
}

function selectedCount() {
  if (TREND_COMPANIES && TREND_COMPANIES.size) return TREND_COMPANIES.size;
  return (TREND_PICKER && TREND_PICKER.all_companies || []).length;
}

function drawCompanyTrends() {
  const children = [];

  // --- The company selector (compact dropdown) + metric toggle, one row ---
  children.push(buildCompanyBar());

  // --- Empty state (friendly, never an error) ---
  if (!TRENDS || TRENDS.empty) {
    children.push(el('div', { class: 'empty' }, [
      el('h3', {}, 'Not enough history yet'),
      el('p', {}, 'Trends take shape once a phase has a few checks behind it. Keep running checks and the line fills in here — how open roles rise and fall, and what’s being added and removed.'),
    ]));
    mountTrendsBody(el('div', {}, children));
    return;
  }

  const selected = committedCompanyKeys();
  const single = selected.length === 1;

  // The series we CAN draw as breakdown lines, and the synthesized total.
  const breakdownSeries = TRENDS.series || [];
  const totalSeries = TRENDS.total_series || null;

  // Decide what actually gets drawn:
  //   • combined (default): just the total line.
  //   • breakdown: every breakdown series PLUS the total as a faint backdrop.
  let toDraw, leadForReadout;
  if (TREND_BREAKDOWN && breakdownSeries.length) {
    toDraw = breakdownSeries.slice();
    if (totalSeries) toDraw = [{ ...totalSeries }, ...toDraw];
    leadForReadout = totalSeries || breakdownSeries[0];
  } else {
    toDraw = totalSeries ? [totalSeries] : breakdownSeries.slice(0, 1);
    leadForReadout = toDraw[0];
  }

  // --- The chart ---
  const chartWrap = el('div', { class: 'trend-chart-wrap' });
  children.push(chartWrap);
  // Drawn AFTER mount: the chart sizes itself to the container's real width, so
  // it fills the panel instead of floating at a data-derived width.
  queueMicrotask(() => mountTrendChart(chartWrap, toDraw));

  // --- Legend (only in breakdown; doubles as hover-to-isolate) ---
  if (TREND_BREAKDOWN && toDraw.length > 1) {
    children.push(buildTrendLegend(toDraw));
  }

  // --- Under-chart controls: split (single company only) + breakdown toggle ---
  children.push(buildTrendRangeControls());
  children.push(buildUnderChartControls(single, breakdownSeries.length));

  // --- Plain-language readout of the lead line ---
  const lead = trendSentence(leadForReadout, TREND_METRIC);
  if (lead) children.push(el('p', { class: 'trend-readout' }, lead));

  mountTrendsBody(el('div', {}, children));
}

/* The top bar: the compact company dropdown on the left, the metric toggle on
   the right. Replaces the old full-height tickbox panel. */
function buildCompanyBar() {
  const bar = el('div', { class: 'trend-bar' });

  // Company dropdown (label = summary of the committed selection).
  bar.appendChild(buildCompanyDropdown());

  // Metric: Open · Added · Removed.
  const metricSeg = el('div', { class: 'toggle' });
  [['open', 'Open roles'], ['added', 'Added'], ['removed', 'Removed']].forEach(([m, lbl]) => {
    metricSeg.appendChild(el('button', {
      class: TREND_METRIC === m ? 'on' : '',
      onclick: () => { if (TREND_METRIC !== m) { TREND_METRIC = m; loadCompanyTrends(); } },
    }, lbl));
  });
  bar.appendChild(el('div', { class: 'trend-ctl' }, [
    el('span', { class: 'trend-ctl-label' }, 'Show'), metricSeg,
  ]));

  // Phase zoom (only when there's more than one phase of data).
  const phaseList = (TRENDS && TRENDS.phases) || [];
  if (phaseList.length > 1) {
    const sel = el('select', { class: 'trend-select',
      onchange: (e) => { TREND_PHASE = e.target.value; loadCompanyTrends(); } });
    sel.appendChild(el('option', { value: '' }, 'All phases'));
    for (const p of phaseList) {
      const opt = el('option', { value: p.id }, p.name + (p.is_current ? ' (current)' : ''));
      if (p.id === TREND_PHASE) opt.setAttribute('selected', '');
      sel.appendChild(opt);
    }
    bar.appendChild(el('div', { class: 'trend-ctl' }, [
      el('span', { class: 'trend-ctl-label' }, 'Phase'), sel,
    ]));
  }

  return bar;
}

/* The company selector: a button that shows the current selection summary and
   opens a floating panel with a search box, a nested bucket→sub-bucket→company
   checkbox tree, and an Apply button. The panel stays open until an outside
   click; ticking edits a DRAFT set, and Apply commits it to the chart. */
function buildCompanyDropdown() {
  const n = selectedCount();
  const total = (TREND_PICKER && TREND_PICKER.all_companies || []).length;
  const summary = (!TREND_COMPANIES || TREND_COMPANIES.size === 0 || n === total)
    ? 'All companies'
    : (n === 1
        ? oneCompanyName([...TREND_COMPANIES][0])
        : `${n} companies`);

  const wrap = el('div', { class: 'trend-ctl trend-dd-ctl' });
  wrap.appendChild(el('span', { class: 'trend-ctl-label' }, 'Companies'));

  const dd = el('div', { class: 'trend-dd' + (TREND_DD_OPEN ? ' open' : '') });
  const btn = el('button', {
    class: 'trend-dd-btn', 'aria-haspopup': 'true',
    'aria-expanded': TREND_DD_OPEN ? 'true' : 'false',
    onclick: (e) => { e.stopPropagation(); toggleCompanyDropdown(); },
  }, [
    el('span', { class: 'trend-dd-summary' }, summary),
    el('span', { class: 'trend-dd-caret' }, '▾'),
  ]);
  dd.appendChild(btn);

  if (TREND_DD_OPEN) dd.appendChild(buildCompanyDropdownPanel());
  wrap.appendChild(dd);
  return wrap;
}

function oneCompanyName(key) {
  const all = (TREND_PICKER && TREND_PICKER.all_companies) || [];
  const hit = all.find(c => c.key === key);
  return hit ? hit.name : key;
}

function toggleCompanyDropdown() {
  if (TREND_DD_OPEN) { closeCompanyDropdown(false); return; }
  // Open: seed the draft from the committed selection (all → every key).
  TREND_DD_OPEN = true;
  TREND_DD_SEARCH = '';
  TREND_DRAFT = new Set(committedCompanyKeys());
  drawCompanyTrends();
  // Register the outside-click closer once the panel is in the DOM.
  setTimeout(() => document.addEventListener('mousedown', onDocClickForDropdown), 0);
  // Focus the search box for quick typing.
  setTimeout(() => { const s = $('.trend-dd-search'); if (s) s.focus(); }, 0);
}

function closeCompanyDropdown(commit) {
  if (commit && TREND_DRAFT) {
    const total = (TREND_PICKER && TREND_PICKER.all_companies || []).length;
    // Committing every company (or none) means "all" → store null for clarity.
    TREND_COMPANIES = (TREND_DRAFT.size === 0 || TREND_DRAFT.size === total)
      ? null : new Set(TREND_DRAFT);
    TREND_BREAKDOWN = false;   // a fresh selection resets to the combined view
    TREND_HILITE = null;
  }
  TREND_DD_OPEN = false;
  TREND_DRAFT = null;
  document.removeEventListener('mousedown', onDocClickForDropdown);
  if (commit) loadCompanyTrends();     // reload with the new selection
  else drawCompanyTrends();            // just redraw the closed button
}

function onDocClickForDropdown(e) {
  const panel = $('.trend-dd');
  if (panel && !panel.contains(e.target)) closeCompanyDropdown(false);
}

/* The floating panel: search, tree, Apply. */
function buildCompanyDropdownPanel() {
  const panel = el('div', { class: 'trend-dd-panel', onmousedown: (e) => e.stopPropagation() });

  // Search box — live-filters the tree as you type.
  const search = el('input', {
    class: 'trend-dd-search', type: 'text', placeholder: 'Search companies…',
    'aria-label': 'Search companies',
    value: TREND_DD_SEARCH,
    oninput: (e) => { TREND_DD_SEARCH = e.target.value; refreshDropdownTree(); },
    onkeydown: (e) => { if (e.key === 'Escape') closeCompanyDropdown(false); },
  });
  panel.appendChild(el('div', { class: 'trend-dd-searchwrap' }, search));

  // Quick "select all / clear" line.
  panel.appendChild(el('div', { class: 'trend-dd-quick' }, [
    el('button', { class: 'trend-dd-quickbtn', onclick: () => { selectAllDraft(true); } }, 'Select all'),
    el('span', { class: 'trend-dd-quicksep' }, '·'),
    el('button', { class: 'trend-dd-quickbtn', onclick: () => { selectAllDraft(false); } }, 'Clear'),
  ]));

  // The tree.
  panel.appendChild(el('div', { class: 'trend-dd-tree', id: 'trendDdTree' }, buildDraftTree()));

  // Apply footer.
  panel.appendChild(el('div', { class: 'trend-dd-foot' }, [
    el('span', { class: 'trend-dd-count', id: 'trendDdCount' },
      `${TREND_DRAFT ? TREND_DRAFT.size : 0} selected`),
    el('button', { class: 'trend-dd-apply', onclick: () => closeCompanyDropdown(true) }, 'Apply'),
  ]));

  return panel;
}

/* Re-render just the tree + count on search/tick without closing the panel. */
function refreshDropdownTree() {
  const tree = $('#trendDdTree');
  if (tree) tree.replaceChildren(buildDraftTree());
  const count = $('#trendDdCount');
  if (count) count.textContent = `${TREND_DRAFT ? TREND_DRAFT.size : 0} selected`;
}

function selectAllDraft(on) {
  const keys = matchingKeys();
  if (on) keys.forEach(k => TREND_DRAFT.add(k));
  else keys.forEach(k => TREND_DRAFT.delete(k));
  refreshDropdownTree();
}

/* Keys currently visible under the search term (used by select-all/clear). */
function matchingKeys() {
  const q = TREND_DD_SEARCH.trim().toLowerCase();
  const all = (TREND_PICKER && TREND_PICKER.all_companies) || [];
  return all.filter(c => !q || (c.name || '').toLowerCase().includes(q)).map(c => c.key);
}

/* Build the nested checkbox tree against TREND_DRAFT, filtered by search. A
   bucket/sub-bucket header checkbox toggles all its (visible) members. */
function buildDraftTree() {
  const q = TREND_DD_SEARCH.trim().toLowerCase();
  const matches = (name) => !q || (name || '').toLowerCase().includes(q);
  const frag = el('div', {});

  const cbox = (key, name) => {
    const on = TREND_DRAFT.has(key);
    return el('label', { class: 'trend-dd-co' + (on ? ' on' : '') }, [
      el('input', { type: 'checkbox', ...(on ? { checked: '' } : {}),
        onchange: (e) => { e.target.checked ? TREND_DRAFT.add(key) : TREND_DRAFT.delete(key); refreshDropdownTree(); } }),
      el('span', {}, name),
    ]);
  };

  const buckets = (TREND_PICKER && TREND_PICKER.buckets) || [];
  if (!buckets.length) {
    const flat = el('div', { class: 'trend-dd-cos' });
    for (const c of matchingKeysAsObjs()) flat.appendChild(cbox(c.key, c.name));
    frag.appendChild(flat);
    return frag;
  }

  let anyShown = false;
  for (const b of buckets) {
    // Visible members of this bucket (across sub-buckets) under the search term.
    const visSubs = [];
    const bucketVisKeys = [];
    for (const sub of b.sub_buckets) {
      const vis = sub.companies.filter(c => matches(c.name));
      if (!vis.length) continue;
      vis.forEach(c => bucketVisKeys.push(c.key));
      visSubs.push({ name: sub.name, companies: vis });
    }
    if (!bucketVisKeys.length) continue;    // whole bucket filtered out
    anyShown = true;

    const bucketAllOn = bucketVisKeys.every(k => TREND_DRAFT.has(k));
    const subBlocks = [];
    for (const sub of visSubs) {
      const subKeys = sub.companies.map(c => c.key);
      const subAllOn = subKeys.every(k => TREND_DRAFT.has(k));
      const named = visSubs.length > 1 || sub.name !== 'Other/Misc';
      const cos = el('div', { class: 'trend-dd-cos' });
      for (const c of sub.companies) cos.appendChild(cbox(c.key, c.name));
      subBlocks.push(el('div', { class: 'trend-dd-sub' }, [
        named ? el('label', { class: 'trend-dd-sublabel' + (subAllOn ? ' on' : '') }, [
          el('input', { type: 'checkbox', ...(subAllOn ? { checked: '' } : {}),
            onchange: (e) => { const on = e.target.checked; subKeys.forEach(k => on ? TREND_DRAFT.add(k) : TREND_DRAFT.delete(k)); refreshDropdownTree(); } }),
          el('span', {}, sub.name),
        ]) : null,
        cos,
      ]));
    }

    frag.appendChild(el('div', { class: 'trend-dd-bucket' }, [
      el('label', { class: 'trend-dd-blabel' + (bucketAllOn ? ' on' : '') }, [
        el('input', { type: 'checkbox', ...(bucketAllOn ? { checked: '' } : {}),
          onchange: (e) => { const on = e.target.checked; bucketVisKeys.forEach(k => on ? TREND_DRAFT.add(k) : TREND_DRAFT.delete(k)); refreshDropdownTree(); } }),
        el('span', {}, b.name),
      ]),
      ...subBlocks,
    ]));
  }

  if (!anyShown) frag.appendChild(el('div', { class: 'trend-dd-none' }, 'No companies match.'));
  return frag;
}

function matchingKeysAsObjs() {
  const q = TREND_DD_SEARCH.trim().toLowerCase();
  const all = (TREND_PICKER && TREND_PICKER.all_companies) || [];
  return all.filter(c => !q || (c.name || '').toLowerCase().includes(q));
}

/* Under-chart controls: the by-team/by-location split (single company only),
   and the breakdown toggle (multi-company or single-with-splits). */
function buildUnderChartControls(single, breakdownCount) {
  const row = el('div', { class: 'trend-under' });

  // Split: By team · By location — only meaningful for ONE company.
  if (single) {
    const axisSeg = el('div', { class: 'toggle' }, [
      el('button', { class: TREND_AXIS === 'department' ? 'on' : '',
        onclick: () => { if (TREND_AXIS !== 'department') { TREND_AXIS = 'department'; TREND_HILITE = null; loadCompanyTrends(); } },
      }, 'By team'),
      el('button', { class: TREND_AXIS === 'location' ? 'on' : '',
        onclick: () => { if (TREND_AXIS !== 'location') { TREND_AXIS = 'location'; TREND_HILITE = null; loadCompanyTrends(); } },
      }, 'By location'),
    ]);
    row.appendChild(el('div', { class: 'trend-ctl' }, [
      el('span', { class: 'trend-ctl-label' }, single ? 'Split' : ''), axisSeg,
    ]));
  }

  // Breakdown toggle — show the individual lines behind the total.
  if (breakdownCount > 1) {
    row.appendChild(el('button', {
      class: 'trend-breakdown-btn' + (TREND_BREAKDOWN ? ' on' : ''),
      onclick: () => { TREND_BREAKDOWN = !TREND_BREAKDOWN; TREND_HILITE = null; drawCompanyTrends(); },
    }, TREND_BREAKDOWN
      ? (single ? 'Hide breakdown' : 'Hide the breakdown')
      : (single
          ? (TREND_AXIS === 'department' ? 'Show teams' : 'Show locations')
          : 'Show each company')));
  }

  return row;
}

/* The legend for breakdown view: one entry per line, hover to isolate it (dims
   the others). The total line reads "All selected" and sits first. */
function buildTrendLegend(toDraw) {
  const wrap = el('div', { class: 'trend-legend' });
  toDraw.forEach((s, i) => {
    const isTotal = !!s.is_total;
    const entry = el('button', {
      class: 'trend-legend-item' + (isTotal ? ' total' : '')
        + (TREND_HILITE && TREND_HILITE !== s.label ? ' dim' : ''),
      onmouseenter: () => setHilite(s.label),
      onmouseleave: () => setHilite(null),
      onfocus: () => setHilite(s.label),
      onblur: () => setHilite(null),
    }, [
      el('span', { class: 'trend-legend-swatch',
        style: `background:${isTotal ? 'var(--ink-soft)' : trendColor(i - (toDraw[0].is_total ? 1 : 0))}` }),
      el('span', { class: 'trend-legend-name' }, s.label),
      el('span', { class: 'trend-legend-val' }, String(s.total_latest)),
    ]);
    wrap.appendChild(entry);
  });
  return wrap;
}

/* Hovering a legend entry dims the other lines. We only redraw the chart SVG,
   not the whole tab, so hover stays snappy. */
function setHilite(label) {
  TREND_HILITE = label;
  const wrap = $('.trend-chart-wrap');
  if (!wrap) return;
  const toDraw = currentToDraw();
  wrap.replaceChildren(buildTrendChart(toDraw, toDraw));
  // Reflect the dim state on legend entries too.
  document.querySelectorAll('.trend-legend-item').forEach(node => {
    const name = node.querySelector('.trend-legend-name');
    if (!name) return;
    const dim = TREND_HILITE && TREND_HILITE !== name.textContent;
    node.classList.toggle('dim', !!dim);
  });
}

/* Recompute the drawn series list from current state (used by hover redraw). */
function currentToDraw() {
  const breakdownSeries = (TRENDS && TRENDS.series) || [];
  const totalSeries = (TRENDS && TRENDS.total_series) || null;
  if (TREND_BREAKDOWN && breakdownSeries.length) {
    let arr = breakdownSeries.slice();
    if (totalSeries) arr = [{ ...totalSeries }, ...arr];
    return arr;
  }
  return totalSeries ? [totalSeries] : breakdownSeries.slice(0, 1);
}

function buildLocationFilter() {
  const locs = TREND_LOCATIONS || [];
  const wrap = el('div', { class: 'trend-ctl trend-loc trend-loc-page' });
  wrap.appendChild(el('span', { class: 'trend-ctl-label' }, 'Location'));
  const chips = el('div', { class: 'trend-loc-chips' });
  if (!locs.length) {
    chips.appendChild(el('span', { class: 'trend-loc-empty' }, 'Anywhere'));
  } else {
    locs.forEach((l, i) => {
      chips.appendChild(el('span', { class: 'trend-loc-chip' }, [
        l,
        el('button', { class: 'trend-loc-x', title: `Remove ${l}`,
          onclick: () => { TREND_LOCATIONS.splice(i, 1); reloadActiveTrends(); } }, '×'),
      ]));
    });
  }
  const input = el('input', { class: 'trend-loc-input', type: 'text',
    placeholder: 'Add a city…', 'aria-label': 'Add a location filter',
    onkeydown: (e) => {
      if (e.key === 'Enter') {
        const v = e.target.value.trim();
        if (v && !(TREND_LOCATIONS || []).some(x => x.toLowerCase() === v.toLowerCase())) {
          (TREND_LOCATIONS = TREND_LOCATIONS || []).push(v);
          reloadActiveTrends();
        }
        e.target.value = '';
      }
    } });
  chips.appendChild(input);
  wrap.appendChild(chips);
  if (JSON.stringify((TREND_LOCATIONS || []).slice().sort())
      !== JSON.stringify((TREND_LOC_SAVED || []).slice().sort())) {
    wrap.appendChild(el('button', { class: 'trend-loc-reset',
      onclick: () => { TREND_LOCATIONS = (TREND_LOC_SAVED || []).slice(); reloadActiveTrends(); } },
      'Reset to saved'));
  }
  return wrap;
}

/* The page-level location filter drives whichever sub-tab is active. */
function reloadActiveTrends() {
  if (TRENDS_TAB === 'company') { TREND_HILITE = null; loadCompanyTrends(); }
  else loadTrackerTrends();
}
/* =======================================================================
   MY-APPLICATIONS sub-tab — a funnel up top, trend lines + rates below.
   ======================================================================= */

async function loadTrackerTrends() {
  const qs = new URLSearchParams();
  if (TRACKER_PHASE) qs.set('phase', TRACKER_PHASE);
  if (TRACKER_BUCKET) qs.set('bucket', TRACKER_BUCKET);
  if (TRACKER_SUB) qs.set('sub_bucket', TRACKER_SUB);
  try {
    TRACKER_TRENDS = await api.get('/api/tracker-trends?' + qs.toString());
  } catch (e) {
    mountTrendsBody(errorBox('Couldn’t read your application history.', e.message));
    return;
  }
  drawTrackerTrends();
}

function drawTrackerTrends() {
  const t = TRACKER_TRENDS;
  const children = [];

  // --- Filters: phase · bucket · sub-bucket ---
  children.push(buildTrackerFilters());

  if (!t || t.empty) {
    children.push(el('div', { class: 'empty' }, [
      el('h3', {}, 'No applications yet this phase'),
      el('p', {}, 'Once you mark roles as applied — from the Jobs tab or by adding them in the tracker — this is where you’ll see how your hunt is going: how many you’ve applied to, how far they progress, and your week-by-week pace.'),
    ]));
    mountTrendsBody(el('div', {}, children));
    return;
  }

  // --- The funnel ---
  children.push(el('div', { class: 'sec-head', style: 'margin:14px 0 8px' }, [
    el('h2', { style: 'font-size:15px' }, 'How your applications progress'),
    el('span', { class: 'hint' }, `${t.total} application${t.total === 1 ? '' : 's'} in view`),
  ]));
  children.push(buildFunnel(t.funnel, t.total));

  // --- Rates (all-time, within phase) + windowed counts, side by side ---
  children.push(buildTrackerStats(t));

  // --- Weekly applications-over-time line ---
  if (t.weekly && t.weekly.length) {
    children.push(el('div', { class: 'sec-head', style: 'margin:18px 0 8px' }, [
      el('h2', { style: 'font-size:15px' }, 'Applications over time'),
      el('span', { class: 'hint' }, 'Per week'),
    ]));
    children.push(el('div', { class: 'trend-chart-wrap' }, buildWeeklyChart(t.weekly)));
  }

  mountTrendsBody(el('div', {}, children));
}

function buildTrackerFilters() {
  const t = TRACKER_TRENDS || {};
  const row = el('div', { class: 'trend-controls-row' });

  // Phase picker.
  const sel = el('select', { class: 'trend-select',
    onchange: (e) => { TRACKER_PHASE = e.target.value; TRACKER_BUCKET = ''; TRACKER_SUB = ''; loadTrackerTrends(); } });
  sel.appendChild(el('option', { value: '' }, t.phase && t.phase.is_current
    ? `${t.phase.name} (current)` : 'Current phase'));
  // Offer other phases if the company tab has loaded them.
  for (const p of (TRENDS && TRENDS.phases || [])) {
    if (t.phase && p.id === t.phase.id) continue;
    const opt = el('option', { value: p.id }, p.name + (p.is_current ? ' (current)' : ''));
    if (p.id === TRACKER_PHASE) opt.setAttribute('selected', '');
    sel.appendChild(opt);
  }
  row.appendChild(el('div', { class: 'trend-ctl' }, [
    el('span', { class: 'trend-ctl-label' }, 'Phase'), sel,
  ]));

  // Bucket filter.
  if (t.buckets && t.buckets.length) {
    const bsel = el('select', { class: 'trend-select',
      onchange: (e) => { TRACKER_BUCKET = e.target.value; TRACKER_SUB = ''; loadTrackerTrends(); } });
    bsel.appendChild(el('option', { value: '' }, 'All buckets'));
    for (const b of t.buckets) {
      const opt = el('option', { value: b }, b);
      if (b === TRACKER_BUCKET) opt.setAttribute('selected', '');
      bsel.appendChild(opt);
    }
    row.appendChild(el('div', { class: 'trend-ctl' }, [
      el('span', { class: 'trend-ctl-label' }, 'Bucket'), bsel,
    ]));
  }

  // Sub-bucket filter (only when a bucket with sub-buckets is chosen).
  if (TRACKER_BUCKET && t.sub_buckets && t.sub_buckets.length > 1) {
    const ssel = el('select', { class: 'trend-select',
      onchange: (e) => { TRACKER_SUB = e.target.value; loadTrackerTrends(); } });
    ssel.appendChild(el('option', { value: '' }, 'All sub-buckets'));
    for (const s of t.sub_buckets) {
      const opt = el('option', { value: s }, s);
      if (s === TRACKER_SUB) opt.setAttribute('selected', '');
      ssel.appendChild(opt);
    }
    row.appendChild(el('div', { class: 'trend-ctl' }, [
      el('span', { class: 'trend-ctl-label' }, 'Sub-bucket'), ssel,
    ]));
  }
  return row;
}

/* A horizontal funnel: each stage a bar whose width is its share of the total,
   labelled with the count and the % of applications that reached it. */
function buildFunnel(funnel, total) {
  const wrap = el('div', { class: 'funnel' });
  const top = (funnel[0] && funnel[0].count) || total || 1;
  funnel.forEach((f, i) => {
    const pct = top ? Math.round((f.count / top) * 100) : 0;
    wrap.appendChild(el('div', { class: 'funnel-row' }, [
      el('div', { class: 'funnel-label' }, f.label),
      el('div', { class: 'funnel-track' }, [
        el('div', { class: 'funnel-bar funnel-bar-' + f.stage,
          style: `width:${Math.max(pct, f.count ? 6 : 0)}%` }),
      ]),
      el('div', { class: 'funnel-val' }, [
        el('span', { class: 'funnel-count' }, String(f.count)),
        i > 0 ? el('span', { class: 'funnel-pct' }, `${pct}%`) : null,
      ]),
    ]));
  });
  return wrap;
}

/* Rates (all-time, within phase) and windowed applied-counts, side by side. */
function buildTrackerStats(t) {
  const pct = (v) => `${Math.round((v || 0) * 100)}%`;
  const rates = t.rates || {};
  const win = t.windows || {};

  const rateCards = el('div', { class: 'stat-cards' }, [
    statCard('Response rate', pct(rates.response_rate), 'reached screening or beyond'),
    statCard('Interview rate', pct(rates.interview_rate), 'reached an interview'),
    statCard('Offer rate', pct(rates.offer_rate), 'ended in an offer'),
    statCard('Ghost rate', pct(rates.ghost_rate), 'went silent 14+ days'),
  ]);

  const winCards = el('div', { class: 'stat-cards' }, [
    statCard('All time', String(win.all != null ? win.all : t.total), 'this phase'),
    statCard('Last 14 days', String(win['14'] || 0), 'applied'),
    statCard('Last 7 days', String(win['7'] || 0), 'applied'),
  ]);

  return el('div', { class: 'tracker-stats' }, [
    el('div', { class: 'tracker-stats-col' }, [
      el('div', { class: 'tracker-stats-h' }, 'Rates (all-time, this phase)'),
      rateCards,
    ]),
    el('div', { class: 'tracker-stats-col' }, [
      el('div', { class: 'tracker-stats-h' }, 'Applications submitted'),
      winCards,
    ]),
  ]);
}

function statCard(label, value, sub) {
  return el('div', { class: 'stat-card' }, [
    el('div', { class: 'stat-value' }, value),
    el('div', { class: 'stat-label' }, label),
    sub ? el('div', { class: 'stat-sub' }, sub) : null,
  ]);
}

/* A small SVG bar chart of applications per week. Same hand-rolled, no-library
   approach as the line chart, themed through --accent. */
function buildWeeklyChart(weekly) {
  const SVGNS = 'http://www.w3.org/2000/svg';
  // The date row lives in its own strip BELOW the plot. padB reserves that whole
  // strip (baseline gap + label height) so a date can never sit on a bar.
  const padL = 30, padR = 14, padT = 16, padB = 54;
  const barW = Math.max(18, Math.min(54, 420 / Math.max(1, weekly.length)));
  const gap = 12;
  const plotW = weekly.length * (barW + gap) + gap;
  const W = padL + plotW + padR, H = 244, plotH = H - padT - padB;
  const maxV = Math.max(1, ...weekly.map(w => w.count));
  const niceMax = niceCeil(maxV);
  const y = (v) => padT + plotH - (v / niceMax) * plotH;
  const baseline = y(0);   // where bars sit and the axis rule is drawn

  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('class', 'trend-svg');
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');
  svg.setAttribute('role', 'img');
  const mk = (tag, attrs, text) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };
  [0, Math.round(niceMax / 2), niceMax].forEach(tk => {
    svg.appendChild(mk('line', { x1: padL, y1: y(tk), x2: W - padR, y2: y(tk), class: 'trend-grid' }));
    svg.appendChild(mk('text', { x: padL - 8, y: y(tk) + 4, class: 'trend-axis-label', 'text-anchor': 'end' }, String(tk)));
  });
  // A solid baseline rule separating the plot from the date strip beneath it.
  svg.appendChild(mk('line', { x1: padL, y1: baseline, x2: W - padR, y2: baseline, class: 'trend-axis-rule' }));

  weekly.forEach((w, i) => {
    const x = padL + gap + i * (barW + gap);
    const h = (w.count / niceMax) * plotH;
    const bar = mk('rect', { x, y: y(w.count), width: barW, height: Math.max(0, h),
      rx: 2, class: 'weekly-bar', style: 'fill:var(--accent)' });
    bar.appendChild(mk('title', {}, `Week of ${w.week_start}: ${w.count}`));
    svg.appendChild(bar);
    if (w.count) svg.appendChild(mk('text', { x: x + barW / 2, y: y(w.count) - 6,
      class: 'trend-point-val', 'text-anchor': 'middle' }, String(w.count)));
    // Week label (MM-DD) in the strip below the baseline — clear of every bar.
    svg.appendChild(mk('text', { x: x + barW / 2, y: baseline + 24,
      class: 'trend-xlabel', 'text-anchor': 'middle' }, w.week_start.slice(5)));
  });
  return svg;
}
/* A small, stable palette that sits on the warm-paper theme. Index-stable so a
   line keeps its colour as you toggle others. */
const TREND_PALETTE = [
  '#C8780C', // signal amber-gold
  '#3E4A52', // slate
  '#4E6B4A', // ok green
  '#8C3A28', // brick
  '#7A5C9E', // muted violet
  '#2F6E78', // teal
  '#A6791C', // ochre
  '#5C544A', // ink-soft
];
function trendColor(i) { return TREND_PALETTE[((i % TREND_PALETTE.length) + TREND_PALETTE.length) % TREND_PALETTE.length]; }

/* Shift a hex colour lighter (amt > 0) or darker (amt < 0), keeping the hue.
   Used to shade each phase band of a series a touch differently so the line
   visibly changes across a phase boundary (the locked Phase-N rule). */
function shadeHex(hex, amt) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  let r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const mix = (c) => amt >= 0 ? Math.round(c + (255 - c) * amt) : Math.round(c * (1 + amt));
  r = Math.max(0, Math.min(255, mix(r)));
  g = Math.max(0, Math.min(255, mix(g)));
  b = Math.max(0, Math.min(255, mix(b)));
  return '#' + [r, g, b].map(c => c.toString(16).padStart(2, '0')).join('');
}
function bandShade(baseHex, bandIndex, bandCount) {
  if (bandCount <= 1) return baseHex;
  const t = bandCount === 1 ? 0 : bandIndex / (bandCount - 1);
  const amt = 0.10 - t * 0.34;
  return shadeHex(baseHex, amt);
}

/* Round a max value up to a tidy axis ceiling (5, 10, 20, 50…). */
function niceCeil(v) {
  if (v <= 5) return 5;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / pow;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return Math.ceil(v / (step * pow)) * (step * pow);
}

/* ISO week key (Monday-anchored) for a YYYY-MM-DD date, and that Monday's date.
   Used when a history is dense enough to switch the x-axis to per-week columns. */
function mondayOf(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  if (isNaN(d)) return dateStr;
  const day = (d.getDay() + 6) % 7;      // 0 = Monday
  d.setDate(d.getDate() - day);
  return d.toISOString().slice(0, 10);
}

/* Catmull-Rom → cubic Bézier smoothing, so the trend line curves gently between
   points (the reference-chart look) instead of hard polyline kinks. Returns an
   SVG path 'd'. Points are [x,y] pairs already in pixel space. */
function smoothPath(pts) {
  if (!pts.length) return '';
  if (pts.length === 1) return `M${pts[0][0]},${pts[0][1]}`;
  if (pts.length === 2) return `M${pts[0][0]},${pts[0][1]} L${pts[1][0]},${pts[1][1]}`;
  const d = [`M${pts[0][0]},${pts[0][1]}`];
  const k = 0.5;   // tension
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6 * k * 2;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6 * k * 2;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6 * k * 2;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6 * k * 2;
    d.push(`C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`);
  }
  return d.join(' ');
}

/* Build the SVG line chart, styled after the reference: soft gradient fill under
   the lead line, faint vertical guide line on every column, dots on every point,
   a floating value tooltip on hover. Lines are banded by phase (never bridging a
   gap). Past 12 columns the x-axis aggregates to weeks (max 12 weeks shown), the
   line smooths to the trend, but every real check still gets its own dot.

   `shown` and `allSeries` are the same list here; colour is by position. A
   series with is_total draws as the emphasised/total line (ink-soft, gradient
   fill); the rest are the breakdown palette. TREND_HILITE dims non-matching. */
/* ---------------------------------------------------------------------------
   The company-hiring chart  (rewritten 2026-07-25)
   ---------------------------------------------------------------------------
   WHAT WAS WRONG BEFORE, and what each fix here is for:

   1. THE CHART DIDN'T FILL ITS BOX. The SVG sized itself to its DATA
      (`width = padL + (nCols-1)*colW + padR`) and was pinned left with
      preserveAspectRatio="xMinYMin meet". Five check-dates produced a ~560px
      chart floating in a 2000px+ panel. Now the caller measures the container
      and passes a target width in, and the chart lays out across it. A
      ResizeObserver redraws on resize, so it stays filled.

   2. THE EXTRA DOTS PER WEEK. The old weekly mode drew one COLUMN per week but
      still plotted a dot for every individual check — all at their week's x.
      Several checks in one week therefore stacked VERTICALLY on a single x
      position, reading as if the same week held several conflicting values.
      The x axis is now TIME-proportional, so a dot always sits at its own real
      date and a stack is impossible.

   3. WEEKLY WAS A GUESS, NOT A CHOICE. `const WEEKLY = allDates.size > 12` —
      the chart silently switched to weeks at the 13th check date, and then
      capped at the most recent 12 weeks, quietly hiding history. Both are gone.
      Granularity is now an explicit Day/Week control, and nothing is hidden
      except by the date range the user chooses.

   THE TWO MODES (as specified by the owner):
     • DAY  — one dot per check date, at that date's real position, joined by a
              line. The value is the latest check that day (the server already
              collapses same-day duplicates per cell, so a date carries one
              value per series).
     • WEEK — the SAME per-day dots, still at their real dates, PLUS a weekly
              trend line drawn through one anchor per week: the LAST check in
              that week. So the dots show every reading and the line shows the
              weekly shape, instead of the two disagreeing.

   PHASE BANDS: drawn from each phase's real date extent. Phases are sequential
   by design (only one is current at a time), so they tile the time axis without
   overlapping.
--------------------------------------------------------------------------- */

const TREND_DAY_MS = 86400000;
function dayNum(d) { return Math.floor(Date.parse(String(d).slice(0, 10) + 'T00:00:00Z') / TREND_DAY_MS); }
function dayStr(n) { return new Date(n * TREND_DAY_MS).toISOString().slice(0, 10); }
function todayStr() { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }

/* The selectable date window: from when the phase was set up, to today. Falls
   back to the data's own extent when the phase has no start recorded. */
function trendRangeBounds() {
  let lo = (HOME && HOME.phase && HOME.phase.started_on) || null;
  const dataLo = TRENDS && TRENDS.date_min ? TRENDS.date_min : null;
  if (!lo) lo = dataLo;
  if (lo && dataLo && dataLo < lo) lo = dataLo;   // never hide real data
  const hi = todayStr();
  return { lo: lo || hi, hi };
}

/* The window actually in force: the user's choice, clamped to the bounds. */
function effectiveTrendRange() {
  const b = trendRangeBounds();
  let from = (TREND_RANGE && TREND_RANGE.from) || b.lo;
  let to = (TREND_RANGE && TREND_RANGE.to) || b.hi;
  if (from < b.lo) from = b.lo;
  if (to > b.hi) to = b.hi;
  if (from > to) from = b.lo;
  return { from, to };
}

function buildTrendChart(shown, allSeries, targetW) {
  const SVGNS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs, text) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };

  const WEEKLY = (TREND_GRAIN === 'week');
  const range = effectiveTrendRange();

  // Colour: the total line is emphasised; breakdown lines take the palette.
  const colorOf = {};
  let ci = 0;
  for (const s of shown) colorOf[s.label] = s.is_total ? 'var(--ink)' : trendColor(ci++);

  // ---- Gather every point inside the chosen window --------------------------
  // Structure kept per (series, band) so phase bands and colours still work.
  const inRange = (d) => d >= range.from && d <= range.to;
  const seriesPts = [];        // {s, bandIdx, bandCount, phaseId, pts:[{date,value}]}
  const phaseExtent = new Map(); // phaseId -> {name,type,lo,hi}
  for (const s of shown) {
    s.bands.forEach((b, bandIdx) => {
      const pts = (b.points || []).filter(p => inRange(p.date))
        .sort((p, q) => p.date < q.date ? -1 : p.date > q.date ? 1 : 0);
      if (!pts.length) return;
      seriesPts.push({ s, bandIdx, bandCount: s.bands.length, phaseId: b.phase_id, pts });
      const rec = phaseExtent.get(b.phase_id)
        || { name: b.phase_name, type: b.phase_type, lo: pts[0].date, hi: pts[0].date };
      if (pts[0].date < rec.lo) rec.lo = pts[0].date;
      if (pts[pts.length - 1].date > rec.hi) rec.hi = pts[pts.length - 1].date;
      phaseExtent.set(b.phase_id, rec);
    });
  }

  const W = Math.max(360, Math.round(targetW || 900));
  const H = 300;
  const padL = 46, padR = 22, padT = 18, padB = 56;
  const plotW = Math.max(1, W - padL - padR);
  const plotH = H - padT - padB;

  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('class', 'trend-svg');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', H);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');

  if (!seriesPts.length) {
    svg.appendChild(mk('text', { x: W / 2, y: H / 2, class: 'trend-xlabel', 'text-anchor': 'middle' },
      'No checks in this date range'));
    return svg;
  }

  // ---- Scales ---------------------------------------------------------------
  // x is TIME-proportional, so every dot sits at its own real date (fix #2).
  const d0 = dayNum(range.from), d1 = dayNum(range.to);
  const span = Math.max(1, d1 - d0);
  const x = (dateStr) => padL + ((dayNum(dateStr) - d0) / span) * plotW;

  let maxV = 0;
  for (const sp of seriesPts) for (const p of sp.pts) maxV = Math.max(maxV, p.value);
  const niceMax = niceCeil(Math.max(1, maxV));
  const y = (v) => padT + plotH - (v / niceMax) * plotH;

  // ---- gradient def (soft fill under the lead line) ----
  const defs = mk('defs', {});
  const grad = mk('linearGradient', { id: 'trendFill', x1: '0', y1: '0', x2: '0', y2: '1' });
  grad.appendChild(mk('stop', { offset: '0', 'stop-color': 'var(--accent)', 'stop-opacity': '0.22' }));
  grad.appendChild(mk('stop', { offset: '1', 'stop-color': 'var(--accent)', 'stop-opacity': '0' }));
  defs.appendChild(grad);
  svg.appendChild(defs);

  // ---- gridlines + y labels ----
  [...new Set([0, Math.round(niceMax / 2), niceMax])].forEach(t => {
    svg.appendChild(mk('line', { x1: padL, y1: y(t), x2: W - padR, y2: y(t), class: 'trend-grid' }));
    svg.appendChild(mk('text', { x: padL - 8, y: y(t) + 4, class: 'trend-axis-label', 'text-anchor': 'end' }, String(t)));
  });
  svg.appendChild(mk('line', { x1: padL, y1: y(0), x2: W - padR, y2: y(0), class: 'trend-axis-rule' }));

  // ---- phase bands (from real date extents; phases are sequential) ----------
  const phases = [...phaseExtent.entries()];
  if (phases.length > 1) {
    phases.forEach(([pid, rec], i) => {
      if (i === 0) return;
      const xs = x(rec.lo) - 6;
      svg.appendChild(mk('line', { x1: xs, y1: padT, x2: xs, y2: y(0), class: 'trend-phase-divider' }));
    });
    phases.forEach(([pid, rec]) => {
      svg.appendChild(mk('text', { x: (x(rec.lo) + x(rec.hi)) / 2, y: H - padB + 30,
        class: 'trend-band-label', 'text-anchor': 'middle' }, rec.name || ''));
    });
  }

  // ---- x labels: evenly spaced across the window, never crowded -------------
  const tickCount = Math.max(2, Math.min(8, Math.floor(plotW / 110)));
  for (let i = 0; i <= tickCount; i++) {
    const dn = d0 + Math.round((span * i) / tickCount);
    const ds = dayStr(dn);
    svg.appendChild(mk('text', { x: x(ds), y: padT + plotH + 18, class: 'trend-xlabel', 'text-anchor': 'middle' },
      ds.slice(5)));
  }

  // ---- the lines ------------------------------------------------------------
  const drawList = seriesPts.slice()
    .sort((a, b) => (a.s.is_total === b.s.is_total) ? 0 : (a.s.is_total ? -1 : 1));

  for (const sp of drawList) {
    const s = sp.s;
    const dim = TREND_HILITE && TREND_HILITE !== s.label;
    const strokeCol = s.is_total ? 'var(--accent)' : bandShade(colorOf[s.label], sp.bandIdx, sp.bandCount);

    // Dots: ALWAYS one per real check date, at its own x. Same in both modes —
    // week mode adds a trend line over them, it doesn't move or merge them.
    const dots = sp.pts.map(p => ({ x: x(p.date), y: y(p.value), p }));

    // The LINE. Day mode joins every dot. Week mode joins one anchor per ISO
    // week — the LAST check in that week, which is the week's current truth and
    // guarantees the line ends on the most recent reading.
    let linePts;
    if (WEEKLY) {
      const perWeek = new Map();
      sp.pts.forEach(p => {
        const wk = mondayOf(p.date);
        const cur = perWeek.get(wk);
        if (!cur || p.date >= cur.date) perWeek.set(wk, p);
      });
      linePts = [...perWeek.values()]
        .sort((a, b) => a.date < b.date ? -1 : 1)
        .map(p => [x(p.date), y(p.value)]);
    } else {
      linePts = dots.map(d => [d.x, d.y]);
    }

    if (s.is_total && linePts.length > 1) {
      const area = smoothPath(linePts)
        + ` L${linePts[linePts.length - 1][0]},${padT + plotH} L${linePts[0][0]},${padT + plotH} Z`;
      svg.appendChild(mk('path', { d: area, fill: 'url(#trendFill)', stroke: 'none',
        class: 'trend-area' + (dim ? ' dim' : '') }));
    }
    if (linePts.length > 1) {
      svg.appendChild(mk('path', {
        d: smoothPath(linePts), fill: 'none', stroke: strokeCol,
        'stroke-width': s.is_total ? '2.6' : '2',
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        class: 'trend-line' + (dim ? ' dim' : ''),
      }));
    }

    dots.forEach((d, k) => {
      const isLast = k === dots.length - 1;
      const dot = mk('circle', { cx: d.x, cy: d.y, r: dots.length === 1 ? 4 : (s.is_total ? 3.4 : 3),
        fill: strokeCol, class: 'trend-dot' + (dim ? ' dim' : '') });
      dot.appendChild(mk('title', {}, `${s.label} · ${d.p.date}: ${d.p.value}`));
      svg.appendChild(dot);
      if (isLast && s.is_total && !dim) {
        svg.appendChild(mk('text', { x: d.x + 6, y: d.y - 7, class: 'trend-point-val', fill: strokeCol },
          String(d.p.value)));
      }
    });
  }

  return svg;
}

/* Draw the chart into `wrap` at the container's real width, and keep it filled
   as the window resizes. Called after the wrap is in the DOM, because an
   unmounted element has clientWidth 0. */
function mountTrendChart(wrap, toDraw) {
  const draw = () => {
    const w = Math.max(360, Math.floor(wrap.clientWidth || 900));
    wrap.replaceChildren(buildTrendChart(toDraw, toDraw, w));
  };
  draw();
  if (window.ResizeObserver) {
    if (wrap._trendRO) wrap._trendRO.disconnect();
    let last = wrap.clientWidth;
    wrap._trendRO = new ResizeObserver(() => {
      const w = wrap.clientWidth;
      if (Math.abs(w - last) < 8) return;   // ignore sub-pixel churn
      last = w;
      draw();
    });
    wrap._trendRO.observe(wrap);
  }
}

/* Granularity (Day/Week) + the date window. The window is bounded by the phase
   start and today, per the owner's spec. */
function buildTrendRangeControls() {
  const b = trendRangeBounds();
  const r = effectiveTrendRange();

  const grain = el('div', { class: 'toggle' },
    [['day', 'Day'], ['week', 'Week']].map(([v, label]) =>
      el('button', {
        class: TREND_GRAIN === v ? 'on' : '',
        type: 'button', 'aria-pressed': String(TREND_GRAIN === v),
        onclick: () => { if (TREND_GRAIN !== v) { TREND_GRAIN = v; drawCompanyTrends(); } },
      }, label)));

  const mkDate = (which, val) => el('input', {
    type: 'date', class: 'trend-date', value: val, min: b.lo, max: b.hi,
    'aria-label': which === 'from' ? 'From date' : 'To date',
    onchange: (e) => {
      const v = e.target.value || null;
      TREND_RANGE = Object.assign({ from: r.from, to: r.to }, TREND_RANGE || {});
      TREND_RANGE[which] = v;
     
      drawCompanyTrends();
    },
  });

  const reset = el('button', {
    class: 'trend-reset', type: 'button',
    onclick: () => { TREND_RANGE = null; drawCompanyTrends(); },
  }, 'Reset');

  const kids = [
    el('div', { class: 'trend-ctl' }, [el('span', { class: 'trend-ctl-label' }, 'By'), grain]),
    el('div', { class: 'trend-ctl' }, [el('span', { class: 'trend-ctl-label' }, 'From'), mkDate('from', r.from)]),
    el('div', { class: 'trend-ctl' }, [el('span', { class: 'trend-ctl-label' }, 'To'), mkDate('to', r.to)]),
  ];
  if (TREND_RANGE) kids.push(reset);
  return el('div', { class: 'trend-range-bar' }, kids);
}

/* A plain-language reading of one series across its phases, phrased for the
   chosen metric. */
function trendSentence(series, metric) {
  if (!series) return '';
  const parts = [];
  for (const b of series.bands) {
    if (!b.points.length) continue;
    const vals = b.points.map(p => p.value);
    const seq = vals.length > 4 ? `${vals[0]} → ${vals[vals.length - 1]}` : vals.join(' → ');
    parts.push(`${seq} in ${b.phase_name}`);
  }
  if (!parts.length) return '';
  const verb = metric === 'added' ? ' added' : metric === 'removed' ? ' removed' : '';
  const name = series.is_total ? 'Total roles' : series.label;
  return `${name}${verb}: ${parts.join(', then ')}.`;
}
/* =======================================================================
   INTERESTS — what you're looking for (E.6)
   Three things shape every report: ranked keywords (order = priority),
   allowed locations (the one hard gate), and an experience ceiling (a gentle
   amber "stretch" flag, never a filter). All read/written via interests.py.
   ======================================================================= */

async function openInterests() {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Loading your preferences…'));
  let data;
  try {
    data = await api.get('/api/interests');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t load your preferences.', e.message));
    return;
  }
  const it = data.interests || {};
  // working copies (no save until the user clicks Save)
  const keywords = (it.keywords_ranked || []).slice();
  const locations = (it.locations_allowed || []).slice();
  let experience = (it.experience_years_max == null) ? '' : String(it.experience_years_max);

  const view = $('#view');

  // --- ranked keywords ---
  const kwList = el('div', { class: 'rank-list' });
  function drawKeywords() {
    kwList.replaceChildren();
    if (!keywords.length) {
      kwList.appendChild(el('p', { class: 'muted', style: 'font-size:13px;margin:6px 0' },
        'No keywords yet. Roles will still show — just unranked.'));
      return;
    }
    keywords.forEach((kw, i) => {
      kwList.appendChild(el('div', { class: 'rank-row' }, [
        el('span', { class: 'rank-num' }, `#${i + 1}`),
        el('span', { class: 'rank-text' }, kw),
        el('div', { class: 'rank-actions' }, [
          el('button', { class: 'mini', title: 'Move up', disabled: i === 0 ? '' : null,
            onclick: () => { [keywords[i-1], keywords[i]] = [keywords[i], keywords[i-1]]; drawKeywords(); } }, '↑'),
          el('button', { class: 'mini', title: 'Move down', disabled: i === keywords.length - 1 ? '' : null,
            onclick: () => { [keywords[i+1], keywords[i]] = [keywords[i], keywords[i+1]]; drawKeywords(); } }, '↓'),
          el('button', { class: 'mini danger', title: 'Remove',
            onclick: () => { keywords.splice(i, 1); drawKeywords(); } }, '×'),
        ]),
      ]));
    });
  }
  drawKeywords();
  const kwInput = el('input', { type: 'text', placeholder: 'e.g. strategy', id: 'kwInput' });
  const kwAdd = el('button', { class: 'btn small' }, 'Add');
  function addKeyword() {
    const v = kwInput.value.trim();
    if (!v) return;
    if (!keywords.some(k => k.toLowerCase() === v.toLowerCase())) keywords.push(v);
    kwInput.value = ''; drawKeywords(); kwInput.focus();
  }
  kwAdd.addEventListener('click', addKeyword);
  kwInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addKeyword(); } });

  // --- locations ---
  const locWrap = el('div', { class: 'bucket-chiprow' });
  function drawLocations() {
    locWrap.replaceChildren();
    if (!locations.length) {
      locWrap.appendChild(el('p', { class: 'muted', style: 'font-size:13px;margin:0' },
        'No locations set — every place is shown. Add one to filter to it.'));
      return;
    }
    locations.forEach((loc, i) => {
      locWrap.appendChild(el('div', { class: 'bucket-chip' }, [
        el('span', { class: 'chip-name' }, loc),
        el('button', { class: 'chip-x', title: 'Remove', onclick: () => { locations.splice(i, 1); drawLocations(); } }, '×'),
      ]));
    });
  }
  drawLocations();
  const locInput = el('input', { type: 'text', placeholder: 'e.g. London, or Remote UK', id: 'locInput' });
  const locAdd = el('button', { class: 'btn small' }, 'Add');
  function addLocation() {
    const v = locInput.value.trim();
    if (!v) return;
    if (!locations.some(l => l.toLowerCase() === v.toLowerCase())) {
      locations.push(v);
      warnIfCityUnrecognised(v);  // Phase O — gentle scope warning (non-blocking)
    }
    locInput.value = ''; drawLocations(); locInput.focus();
  }
  locAdd.addEventListener('click', addLocation);
  locInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addLocation(); } });

  // --- experience ceiling ---
  const expInput = el('input', { type: 'text', placeholder: 'e.g. 8 (leave blank for no limit)', id: 'expInput', value: experience });

  // --- save ---
  const saveBtn = el('button', { class: 'btn signal' }, 'Save preferences');
  saveBtn.addEventListener('click', async () => {
    const expRaw = expInput.value.trim();
    saveBtn.disabled = true;
    try {
      await api.post('/api/interests/save', {
        keywords_ranked: keywords,
        locations_allowed: locations,
        experience_years_max: expRaw === '' ? null : expRaw,
      });
      toast('Preferences saved.');
      loadHome();
    } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
  });

  view.replaceChildren(
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
    el('div', { class: 'sec-head' }, [ el('h2', {}, 'What you’re looking for') ]),
    el('p', { class: 'lead' }, 'These three things shape every report. Nothing here hides roles except locations — keywords just sort what matters to the top, and the experience ceiling only adds a gentle flag.'),

    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Ranked keywords'),
      el('p', { class: 'pref-hint' }, 'Roles matching your higher-ranked words rise to the top. Order matters — #1 outranks #5.'),
      kwList,
      el('div', { class: 'url-row', style: 'margin-top:12px' }, [ kwInput, kwAdd ]),
    ]),

    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Locations'),
      el('p', { class: 'pref-hint' }, 'The one hard filter. Roles clearly elsewhere are hidden; country-only or remote ones are kept for review. Leave empty to see everywhere.'),
      locWrap,
      el('div', { class: 'url-row', style: 'margin-top:12px' }, [ locInput, locAdd ]),
    ]),

    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Experience ceiling'),
      el('p', { class: 'pref-hint' }, 'Roles asking for more years than this get an amber “stretch” flag — never hidden, so you can still apply. Leave blank for no flag.'),
      el('div', { class: 'field', style: 'margin:0;max-width:280px' }, [
        el('label', { for: 'expInput' }, 'Comfortable maximum (years)'),
        expInput,
      ]),
    ]),

    el('div', { class: 'btn-row', style: 'margin-top:24px' }, [ saveBtn ]),
  );
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* =======================================================================
   SETTINGS — adjustable preferences (E.7): the dormancy threshold
   ======================================================================= */

async function openSettings() {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Loading settings…'));
  let s;
  try {
    s = await api.get('/api/settings');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t load settings.', e.message));
    return;
  }
  const input = el('input', { type: 'text', id: 'dormInput', value: String(s.dormancy_days) });
  const saveBtn = el('button', { class: 'btn signal' }, 'Save settings');
  saveBtn.addEventListener('click', async () => {
    const v = input.value.trim();
    saveBtn.disabled = true;
    try {
      await api.post('/api/settings/save', { dormancy_days: v });
      toast('Settings saved.');
      loadHome();
    } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
  });

  // (H) Appearance: Light / Dark / System. Applies and persists immediately on
  // click (no Save needed) so the change is felt at once and survives restarts.
  const MODES = [['light', 'Light'], ['dark', 'Dark'], ['system', 'System']];
  const segBtns = {};
  const apprSeg = el('div', { class: 'appearance-seg', role: 'group', 'aria-label': 'Appearance' },
    MODES.map(([mode, label]) => {
      const b = el('button', {
        class: COLOR_MODE === mode ? 'on' : '',
        'aria-pressed': COLOR_MODE === mode ? 'true' : 'false',
        onclick: async () => {
          await setColorMode(mode);
          for (const [m, btn] of Object.entries(segBtns)) {
            const on = m === mode;
            btn.className = on ? 'on' : '';
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
          }
        },
      }, [ appearanceGlyph(mode), document.createTextNode(label) ]);
      segBtns[mode] = b;
      return b;
    }));

  // End-current-phase control (Post-Phase-O). A deliberate, findable way to end
  // the live phase and drop to dormant — rather than relying on tapping the
  // dormant tile in the switcher. Only actionable when a phase is open; when
  // already resting it shows as disabled with a plain note.
  const hasPhase = !!(HOME && HOME.phase);
  const curPhaseName = hasPhase ? HOME.phase.name : null;
  const endBtn = el('button', {
    class: 'btn danger',
    disabled: hasPhase ? null : 'disabled',
  }, hasPhase ? 'End current phase' : 'No phase to end');
  let endConfirming = false;
  const endBlock = el('div', { class: 'pref-block' });
  const renderEndBlock = () => {
    const kids = [
      el('h3', {}, 'Current phase'),
      el('p', { class: 'pref-hint' }, hasPhase
        ? `You’re in “${curPhaseName}”. Ending it closes this chapter and drops the app to its resting (dormant) state — nothing is deleted, and the phase stays in History. Start a new phase whenever you’re ready.`
        : 'You’re resting (no current phase). Start a phase from the switcher at the top-left whenever you want to begin checking again.'),
    ];
    if (!hasPhase) {
      kids.push(endBtn);
    } else if (!endConfirming) {
      endBtn.onclick = () => { endConfirming = true; renderEndBlock(); };
      kids.push(endBtn);
    } else {
      kids.push(el('div', { class: 'phase-delete-warn' },
        `End “${curPhaseName}” now? You’ll drop to dormant until you start a new phase. Your history is kept.`));
      kids.push(el('div', { class: 'phase-delete-actions' }, [
        el('button', { class: 'btn ghost small', onclick: () => { endConfirming = false; renderEndBlock(); } }, 'Cancel'),
        el('button', { class: 'btn danger small', onclick: endCurrentPhase }, 'End phase'),
      ]));
    }
    endBlock.replaceChildren(...kids);
  };
  renderEndBlock();

  async function endCurrentPhase() {
    try {
      await api.post('/api/phase/end', {});
      toast('Phase ended. Resting now.');
      await refreshAfterPhaseChange();
    } catch (e) { toast(e.message, true); }
  }

  $('#view').replaceChildren(
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
    el('div', { class: 'sec-head' }, [ el('h2', {}, 'Settings') ]),
    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Appearance'),
      el('p', { class: 'pref-hint' },
        'Light, dark, or follow your Mac (System changes with your display, including at sunset). The colours themselves shift with your phase — active, casual, or dormant — this just sets light or dark underneath.'),
      apprSeg,
    ]),
    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Dormancy threshold'),
      el('p', { class: 'pref-hint' },
        `If a phase goes this many days without a check, coming back starts a clean baseline — everything shows as new, with no “200 removed” shock after a long gap. Between ${s.dormancy_min} and ${s.dormancy_max} days. Default is 21.`),
      el('div', { class: 'field', style: 'margin:0;max-width:280px' }, [
        el('label', { for: 'dormInput' }, 'Days of silence before dormant'),
        input,
      ]),
    ]),
    endBlock,
    el('div', { class: 'btn-row', style: 'margin-top:24px' }, [ saveBtn ]),
  );
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* =======================================================================
   GUIDE — how adding companies works (E.7, the honest three-tier model)
   ======================================================================= */

async function openGuide() {
  $('#view').replaceChildren(el('div', { class: 'loading' }, 'Loading…'));
  let g;
  try {
    g = await api.get('/api/guides');
  } catch (e) {
    $('#view').replaceChildren(errorBox('Couldn’t load the guide.', e.message));
    return;
  }
  const t1 = g.tier1_providers || [];
  const t2 = g.tier2_providers || [];

  const providerLine = (arr) => arr.map(p => p.provider).join(', ');

  $('#view').replaceChildren(
    el('button', { class: 'backlink', onclick: loadHome }, '← Home'),
    el('div', { class: 'sec-head' }, [ el('h2', {}, 'How adding companies works') ]),
    el('p', { class: 'lead' }, 'JobWatch is honest about what it can track. When you paste a careers-page URL, one of three things happens:'),

    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Tier 1 — paste and go'),
      el('p', { class: 'pref-hint' }, 'Most companies use a job board JobWatch recognises straight from the URL. Paste it, confirm the name, done — no codes or jargon.'),
      el('p', { class: 'guide-providers' }, [
        el('span', { class: 'muted' }, 'Supported boards: '),
        document.createTextNode(providerLine(t1)),
      ]),
    ]),

    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Tier 2 — built-in presets'),
      el('p', { class: 'pref-hint' }, 'A few big employers have their own custom careers systems, built in as presets. Paste their URL and JobWatch points you at the preset.'),
      el('p', { class: 'guide-providers' }, [
        el('span', { class: 'muted' }, 'Presets: '),
        document.createTextNode(providerLine(t2) || '—'),
      ]),
    ]),

    el('div', { class: 'pref-block' }, [
      el('h3', {}, 'Tier 3 — not yet supported'),
      el('p', { class: 'pref-hint' }, 'If a company uses a board JobWatch doesn’t handle yet, it says so plainly rather than pretending. The URL is noted as a request, and a connector can be built for it later — a short collaboration. Nothing breaks in the meantime.'),
    ]),

    el('div', { class: 'btn-row', style: 'margin-top:24px' }, [
      el('button', { class: 'btn signal', onclick: openManage }, 'Add a company'),
    ]),
  );
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ===========================================================================
   PHASE K — JOBS PAGE
   The place where checks run and roles are read. Three tabs:
     • Check / Run     — last-checked line, "Check jobs" → pick companies → run →
                         two-pane results (75% roles · 25% companies you ran).
     • (Show last run) — the same two-pane view, from the saved last run.
     • Saved jobs      — starred roles; filter by date/company/alphabetical;
                         link out, mark applied (→ tracker, Phase L), or remove.
   The run reuses the engine end-to-end (run_companies); the two-pane "click a
   company → all its roles" view reads the FULL current list the run already
   carried back (result.companies[].current), so there's no second fetch.
   =========================================================================== */

let JOBS = null;             // last /api/jobs payload (phase + pick tree + saved count)
let JOBS_TAB = 'check';      // 'check' | 'saved'
let RUN_RESULT = null;       // the result currently shown in the two-pane view
let RUN_FOCUS = null;        // company key whose full list is shown on the right, or null
let DEPT_VIEW = null;        // transient department VIEW filter on the focused company's
                            // list (a department name, or null = show all). This NEVER
                            // touches the saved/active filter set — it only narrows what's
                            // displayed. Resets on: changing company, picking a different
                            // department, re-clicking the active one, clicking "clear", or
                            // a fresh run. (Post-Phase-O item, locked S27.)
let PICK_BUCKET = null;      // the bucket chosen in the picker, or null
let PICK_SELECTED = new Set; // company keys ticked in the picker
let SAVED_SET = new Set;     // "key\u0000id" of saved roles, for instant star state
let SAVED_LIST = [];         // last /api/saved payload (full records)
let SAVED_SORT = 'date';     // 'date' | 'company' | 'alpha'

// M — the Jobs filter panel state. JOBS_FILTERS_SAVED holds the saved defaults
// (from interests.json); JOBS_FILTERS is the working copy the panel edits for
// THIS run. Running sends JOBS_FILTERS without touching the saved defaults; the
// "Save as my defaults" button writes JOBS_FILTERS back to interests.json.
let JOBS_FILTERS_SAVED = null;
let JOBS_FILTERS = null;
let JOBS_FILTERS_OPEN = false;   // is the panel expanded?
const MAX_LOCATIONS = 5;

const _savedKey = (companyKey, id) => `${companyKey}\u0000${id}`;

/* "June 27th 2026" — the user's chosen long-date format. */
function fmtLongDate(iso) {
  if (!iso) return null;
  const d = new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso);
  if (isNaN(d)) return null;
  const months = ['January','February','March','April','May','June','July',
                  'August','September','October','November','December'];
  const day = d.getDate();
  const ord = (n) => {
    if (n % 100 >= 11 && n % 100 <= 13) return 'th';
    return ({1:'st',2:'nd',3:'rd'})[n % 10] || 'th';
  };
  return `${months[d.getMonth()]} ${day}${ord(day)} ${d.getFullYear()}`;
}

/* "2:05 PM" — the time half of "date and time". */
function fmtTime(iso) {
  if (!iso || iso.length <= 10) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  let h = d.getHours(); const m = String(d.getMinutes()).padStart(2, '0');
  const ap = h < 12 ? 'AM' : 'PM'; h = h % 12 || 12;
  return `${h}:${m} ${ap}`;
}

/* Pull the freshest saved set so stars render correctly. Forgiving. */
async function refreshSaved() {
  try {
    const r = await api.get('/api/saved');
    SAVED_LIST = r.saved || [];
    SAVED_SET = new Set(SAVED_LIST.map(s => _savedKey(s.company_key, s.id)));
  } catch (e) { SAVED_LIST = []; SAVED_SET = new Set; }
}

async function openJobs() {
  const view = $('#view');
  view.replaceChildren(el('div', { class: 'loading' }, 'Reading your jobs…'));
  try {
    JOBS = await api.get('/api/jobs');
  } catch (e) {
    view.replaceChildren(errorBox('Couldn’t load the Jobs page.', e.message));
    return;
  }
  await refreshSaved();
  await loadJobsFilters();

  // Re-attach: if a check is already running (e.g. you left this screen and came
  // back), show the run panel and reconnect the live stream instead of stranding
  // you behind "a check is already running".
  try {
    const st = await api.get('/api/run/status');
    if (st && st.running) {
      renderRunPanel(st.label || 'your check');
      streamJobsRun();
      return;
    }
  } catch (e) { /* status is best-effort; fall through to normal Jobs */ }

  drawJobs(true);
}

/* M — load the saved filter defaults (interests.json) into the working copy.
   Forgiving: any failure yields empty filters so the panel still opens. */
async function loadJobsFilters() {
  let it = { keywords_ranked: [], keywords_mode: 'rank',
             locations_allowed: [], departments_allowed: [],
             departments_mode: 'filter',
             experience_years_max: null };
  try {
    const r = await api.get('/api/interests');
    if (r && r.interests) it = Object.assign(it, r.interests);
  } catch (e) { /* keep empty defaults */ }
  JOBS_FILTERS_SAVED = _cloneFilters(it);
  JOBS_FILTERS = _cloneFilters(it);
}

function _cloneFilters(f) {
  return {
    keywords_ranked: Array.isArray(f.keywords_ranked) ? f.keywords_ranked.slice() : [],
    keywords_mode: f.keywords_mode === 'filter' ? 'filter' : 'rank',
    locations_allowed: Array.isArray(f.locations_allowed) ? f.locations_allowed.slice() : [],
    departments_allowed: Array.isArray(f.departments_allowed) ? f.departments_allowed.slice() : [],
    departments_mode: f.departments_mode === 'rank' ? 'rank' : 'filter',
    experience_years_max: (f.experience_years_max === 0 || f.experience_years_max) ? f.experience_years_max : null,
  };
}

/* Has the working copy diverged from the saved defaults? Drives the "tweaked
   for this run" hint + whether "Save as my defaults" is meaningful. */
function _filtersDiffer() {
  if (!JOBS_FILTERS || !JOBS_FILTERS_SAVED) return false;
  const a = JOBS_FILTERS, b = JOBS_FILTERS_SAVED;
  return JSON.stringify([a.keywords_ranked, a.keywords_mode, a.locations_allowed,
                         a.departments_allowed, a.departments_mode, a.experience_years_max])
       !== JSON.stringify([b.keywords_ranked, b.keywords_mode, b.locations_allowed,
                           b.departments_allowed, b.departments_mode, b.experience_years_max]);
}

/* The Jobs shell: a tab strip (Check · Saved), then the active tab's body. */
function drawJobs(scrollTop = false) {
  const view = $('#view');
  const tabs = el('div', { class: 'jobs-tabs' }, [
    el('button', {
      class: 'jobs-tab' + (JOBS_TAB === 'check' ? ' on' : ''),
      onclick: () => { JOBS_TAB = 'check'; drawJobs(); },
    }, 'Check'),
    el('button', {
      class: 'jobs-tab' + (JOBS_TAB === 'saved' ? ' on' : ''),
      onclick: async () => { JOBS_TAB = 'saved'; await refreshSaved(); drawJobs(); },
    }, [
      'Saved jobs',
      SAVED_SET.size ? el('span', { class: 'tab-badge' }, String(SAVED_SET.size)) : null,
    ]),
  ]);

  const body = JOBS_TAB === 'saved' ? jobsSavedBody() : jobsCheckBody();
  view.replaceChildren(tabs, body);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---- CHECK tab -------------------------------------------------------- */

function jobsCheckBody() {
  // If a run result is in hand, show the two-pane results instead of the landing.
  if (RUN_RESULT) return twoPaneResults(RUN_RESULT);

  const wrap = el('div', { class: 'jobs-landing' });
  const phase = JOBS.phase;

  if (!phase) {
    wrap.appendChild(el('div', { class: 'empty' }, [
      el('h3', {}, 'No active phase'),
      el('p', {}, 'Start a phase from Home to begin a hunt, then come back here to check for new roles.'),
    ]));
    return wrap;
  }

  // Last-checked (date + time, the user's long format) as the centred headline.
  const when = JOBS.last_check_at || (JOBS.last_check ? JOBS.last_check : null);
  const longDate = fmtLongDate(when);
  const time = fmtTime(JOBS.last_check_at);
  const headline = longDate
    ? (time ? `${longDate} · ${time}` : longDate)
    : 'Not checked yet this phase';

  // Centred hero, mirroring the Home page: eyebrow, big headline, then a centred
  // row of actions below. No box — it sits on the page like Home does.
  const actions = el('div', { class: 'home-actions' }, [
    el('button', { class: 'btn signal big', onclick: openPicker }, 'Check jobs'),
  ]);
  // "Show last run" — only when a saved report exists.
  api.get('/api/last-report').then(r => {
    if (r && r.result) {
      actions.appendChild(el('button', {
        class: 'btn ghost big', onclick: showLastRun,
      }, 'Show last run'));
    }
  }).catch(() => {});

  wrap.appendChild(el('div', { class: 'home-hero jobs-hero' }, [
    el('p', { class: 'home-eyebrow' }, 'Last checked'),
    el('p', { class: 'home-q jl-headline' + (longDate ? '' : ' muted') }, headline),
    actions,
  ]));

  return wrap;
}

/* The picker: one bucket, expanded into sub-buckets and companies, with nicely
   styled checkboxes + select-all at sub-bucket and bucket level. Run is pinned
   at the top (sticky) so it's always reachable. */
function openPicker() {
  PICK_BUCKET = null;
  PICK_SELECTED = new Set;
  drawPicker(true);
}

function drawPicker(scrollTop = false) {
  const view = $('#view');
  const runnableBuckets = (JOBS.buckets || []).filter(b => b.runnable_count > 0);

  // Sticky run bar at the very top.
  const count = PICK_SELECTED.size;
  const runBar = el('div', { class: 'pick-runbar' }, [
    el('div', { class: 'pick-runbar-info' }, [
      el('span', { class: 'pick-count' }, String(count)),
      el('span', { class: 'pick-count-word' }, count === 1 ? ' company selected' : ' companies selected'),
    ]),
    el('div', { class: 'btn-row' }, [
      el('button', { class: 'btn ghost', onclick: () => { RUN_RESULT = null; drawJobs(); } }, 'Cancel'),
      el('button', {
        class: 'btn signal rb-run' + (count ? '' : ' is-disabled'),
        disabled: count ? null : '',
        onclick: runSelection,
      }, 'Run'),
    ]),
  ]);

  const head = el('div', { class: 'sec-head' }, [ el('h2', {}, 'Check jobs') ]);

  const filters = filterPanel();

  let chooser;
  if (!PICK_BUCKET) {
    // Step 1 — choose exactly one bucket.
    if (!runnableBuckets.length) {
      chooser = el('div', { class: 'empty' }, [
        el('h3', {}, 'No runnable companies yet'),
        el('p', {}, 'Add some companies under Companies & Buckets, then come back to run a check.'),
      ]);
    } else {
      chooser = el('div', { class: 'pick-buckets' },
        runnableBuckets.map(b => el('button', {
          class: 'pick-bucket', onclick: () => { PICK_BUCKET = b.name; drawPicker(true); },
        }, [
          el('span', { class: 'pb-name' }, b.name),
          el('span', { class: 'pb-meta' }, `${b.runnable_count} ready`),
          el('span', { class: 'pb-go' }, '→'),
        ])));
    }
  } else {
    chooser = pickerBucketDetail(runnableBuckets.find(b => b.name === PICK_BUCKET));
  }

  view.replaceChildren(runBar, head, filters, chooser);
  if (scrollTop) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* M — the filter panel: Location (≤5), Keywords/phrases (multi, rank|filter),
   Department (multi, contains-match). Collapsed to a summary line; expands to
   edit. Edits change JOBS_FILTERS (this run only). "Save as my defaults" writes
   to interests.json. */
function filterPanel() {
  const f = JOBS_FILTERS || _cloneFilters({});
  const tweaked = _filtersDiffer();

  const summaryText = _filterSummaryText(f);

  const header = el('button', {
    class: 'filter-toggle' + (JOBS_FILTERS_OPEN ? ' is-open' : ''),
    onclick: () => { JOBS_FILTERS_OPEN = !JOBS_FILTERS_OPEN; redrawFilterPanel(); },
  }, [
    el('span', { class: 'ft-label' }, 'Filters'),
    el('span', { class: 'ft-summary' }, summaryText),
    tweaked ? el('span', { class: 'ft-tweaked' }, 'tweaked for this run') : null,
    el('span', { class: 'ft-caret' }, JOBS_FILTERS_OPEN ? '▾' : '▸'),
  ]);

  const panel = el('div', { class: 'filter-panel' }, [ header ]);
  if (!JOBS_FILTERS_OPEN) return panel;

  // --- Location (max 5) ---
  panel.appendChild(filterGroup(
    'Location',
    `Up to ${MAX_LOCATIONS}. Roles clearly elsewhere are hidden; country-only ones are kept for review.`,
    f.locations_allowed,
    (val) => {
      if (f.locations_allowed.length >= MAX_LOCATIONS) {
        toast(`You can choose up to ${MAX_LOCATIONS} locations.`, true);
        return false;
      }
      f.locations_allowed.push(val);
      warnIfCityUnrecognised(val);  // Phase O — gentle scope warning (non-blocking)
      return true;
    },
    (val) => { const i = f.locations_allowed.indexOf(val); if (i > -1) f.locations_allowed.splice(i, 1); },
    'Add a location…'
  ));

  // --- Keywords / phrases, with the rank|filter toggle ---
  const kwGroup = filterGroup(
    'Keywords or phrases',
    'Matched against each role’s title and department.',
    f.keywords_ranked,
    (val) => { f.keywords_ranked.push(val); return true; },
    (val) => { const i = f.keywords_ranked.indexOf(val); if (i > -1) f.keywords_ranked.splice(i, 1); },
    'Add a keyword or phrase…'
  );
  // Rank/filter toggle sits under the keyword input.
  kwGroup.appendChild(el('div', { class: 'kw-mode' }, [
    el('span', { class: 'kw-mode-label' }, 'When these match:'),
    modeOption('Rank them', 'rank', f),
    modeOption('Filter to them', 'filter', f),
  ]));
  panel.appendChild(kwGroup);

  // --- Department (multi, contains-match) with its own rank/filter toggle ---
  const deptGroup = filterGroup(
    'Department',
    'Contains-match (“Finance” catches “Global Finance”). See which departments are actually hiring below.',
    f.departments_allowed,
    (val) => { f.departments_allowed.push(val); return true; },
    (val) => { const i = f.departments_allowed.indexOf(val); if (i > -1) f.departments_allowed.splice(i, 1); },
    'Add a department…'
  );
  deptGroup.appendChild(el('div', { class: 'kw-mode' }, [
    el('span', { class: 'kw-mode-label' }, 'When these match:'),
    modeOption('Rank them', 'rank', f, 'departments_mode'),
    modeOption('Filter to them', 'filter', f, 'departments_mode'),
  ]));
  panel.appendChild(deptGroup);

  // --- Footer: run-with hint + save-as-defaults ---
  const footer = el('div', { class: 'filter-footer' }, [
    el('span', { class: 'muted' }, tweaked
      ? 'These apply to this run only.'
      : 'These are your saved defaults.'),
    el('button', {
      class: 'btn ghost small' + (tweaked ? '' : ' is-disabled'),
      disabled: tweaked ? null : '',
      onclick: saveFiltersAsDefaults,
    }, 'Save as my defaults'),
  ]);
  panel.appendChild(footer);

  return panel;
}

/* One labelled multi-add chip group. onAdd(value)->bool (false = rejected, e.g.
   over the cap); onRemove(value)->void. Updates ITS OWN chip row in place — no
   full picker re-draw — so adding/removing a filter never scrolls the page or
   steals focus from the input (the user types, hits Enter, keeps typing). After
   a change it calls refreshFilterSummary() so the collapsed summary + the
   "Save as my defaults" button stay in sync. */
function filterGroup(label, hint, values, onAdd, onRemove, placeholder) {
  const chipRow = el('div', { class: 'chip-row' });

  const makeChip = (v) => el('span', { class: 'chip' }, [
    document.createTextNode(v),
    el('button', {
      class: 'chip-x', 'aria-label': `Remove ${v}`,
      onclick: () => { onRemove(v); chip.remove(); refreshFilterSummary(); },
    }, '×'),
  ]);
  let chip;  // referenced by the closure above per-iteration
  values.forEach(v => { chip = makeChip(v); chipRow.appendChild(chip); });

  const input = el('input', { class: 'filter-input', type: 'text',
    placeholder: placeholder || 'Add…' });

  const commit = () => {
    const val = (input.value || '').trim();
    if (!val) return;
    if (values.includes(val)) { input.value = ''; return; }  // no dupes
    const ok = onAdd(val);
    if (ok === false) return;            // rejected (e.g. over the cap) — keep text
    chip = makeChip(val);
    chipRow.appendChild(chip);           // add the chip in place, no re-draw
    input.value = '';
    input.focus();                       // keep typing the next one
    refreshFilterSummary();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
  });

  return el('div', { class: 'filter-group' }, [
    el('div', { class: 'fg-head' }, [
      el('span', { class: 'fg-label' }, label),
      el('span', { class: 'fg-hint' }, hint),
    ]),
    chipRow,
    el('div', { class: 'fg-add' }, [
      input,
      el('button', { class: 'btn ghost small', onclick: commit }, 'Add'),
    ]),
  ]);
}

/* Update the collapsed summary line + the "tweaked" pill + the "Save as my
   defaults" enabled-state in place, without redrawing the panel (so the open
   editor and the user's scroll position are untouched). Safe to call any time;
   it no-ops if the panel isn't on screen. */
function refreshFilterSummary() {
  const f = JOBS_FILTERS || _cloneFilters({});
  const sum = $('.filter-toggle .ft-summary');
  if (sum) sum.textContent = _filterSummaryText(f);

  const toggle = $('.filter-toggle');
  if (toggle) {
    let pill = toggle.querySelector('.ft-tweaked');
    const tweaked = _filtersDiffer();
    if (tweaked && !pill) {
      pill = el('span', { class: 'ft-tweaked' }, 'tweaked for this run');
      toggle.insertBefore(pill, toggle.querySelector('.ft-caret'));
    } else if (!tweaked && pill) {
      pill.remove();
    }
  }

  // Footer hint + Save button enabled-state.
  const tweaked = _filtersDiffer();
  const hint = $('.filter-footer .muted');
  if (hint) hint.textContent = tweaked
    ? 'These apply to this run only.'
    : 'These are your saved defaults.';
  const saveBtn = $('.filter-footer .btn');
  if (saveBtn) {
    if (tweaked) { saveBtn.classList.remove('is-disabled'); saveBtn.removeAttribute('disabled'); }
    else { saveBtn.classList.add('is-disabled'); saveBtn.setAttribute('disabled', ''); }
  }
}

function _filterSummaryText(f) {
  const bits = [];
  if (f.locations_allowed.length) bits.push(`${f.locations_allowed.length} location${f.locations_allowed.length > 1 ? 's' : ''}`);
  if (f.keywords_ranked.length) bits.push(`${f.keywords_ranked.length} keyword${f.keywords_ranked.length > 1 ? 's' : ''} (${f.keywords_mode})`);
  if (f.departments_allowed.length) bits.push(`${f.departments_allowed.length} department${f.departments_allowed.length > 1 ? 's' : ''}`);
  return bits.length ? bits.join(' · ') : 'No filters — every role in your locations shows';
}

/* Swap just the filter panel in place (expand/collapse, mode toggle) without
   redrawing the whole picker or scrolling. Keeps the user where they are. */
function redrawFilterPanel() {
  const existing = $('.filter-panel');
  if (!existing) { drawPicker(); return; }   // panel not on screen — fall back
  existing.replaceWith(filterPanel());
}

function modeOption(label, value, f, field = 'keywords_mode') {
  const on = f[field] === value;
  return el('button', {
    class: 'mode-opt' + (on ? ' is-on' : ''),
    onclick: () => { f[field] = value; redrawFilterPanel(); },
  }, [
    el('span', { class: 'mode-dot' + (on ? ' is-on' : '') }),
    el('span', {}, label),
  ]);
}

async function saveFiltersAsDefaults() {
  try {
    const f = JOBS_FILTERS;
    const r = await api.post('/api/interests/save', {
      keywords_ranked: f.keywords_ranked,
      keywords_mode: f.keywords_mode,
      locations_allowed: f.locations_allowed,
      departments_allowed: f.departments_allowed,
      departments_mode: f.departments_mode,
      experience_years_max: f.experience_years_max,
    });
    if (r && r.interests) {
      JOBS_FILTERS_SAVED = _cloneFilters(r.interests);
      JOBS_FILTERS = _cloneFilters(r.interests);
    }
    toast('Saved as your default filters.');
    redrawFilterPanel();
  } catch (e) { toast(e.message, true); }
}

function pickerBucketDetail(bucket) {
  if (!bucket) { PICK_BUCKET = null; return el('div', {}); }

  const wrap = el('div', { class: 'pick-detail' });

  // Bucket header with "select all sub-buckets in this bucket".
  const allKeys = [];
  bucket.sub_buckets.forEach(s => s.companies.forEach(c => { if (c.runnable) allKeys.push(c.key); }));
  const allOn = allKeys.length && allKeys.every(k => PICK_SELECTED.has(k));

  const bucketAll = selectAllControl('All in bucket', allOn, () => toggleKeys(allKeys, !isAllOn(allKeys)));
  bucketAll.dataset.allFor = allKeys.join(',');   // hook: refresh this control in place

  wrap.appendChild(el('div', { class: 'pick-bucket-head' }, [
    el('button', { class: 'backlink', onclick: () => { PICK_BUCKET = null; drawPicker(true); } }, '← Buckets'),
    el('div', { class: 'pbh-title' }, bucket.name),
    bucketAll,
  ]));

  // Each sub-bucket: header (select-all) + its companies.
  bucket.sub_buckets.forEach(sub => {
    const subKeys = sub.companies.filter(c => c.runnable).map(c => c.key);
    const subOn = subKeys.length && subKeys.every(k => PICK_SELECTED.has(k));
    const subAll = subKeys.length ? selectAllControl('All', subOn, () => toggleKeys(subKeys, !isAllOn(subKeys))) : null;
    if (subAll) subAll.dataset.allFor = subKeys.join(',');   // hook
    const block = el('div', { class: 'pick-sub' }, [
      el('div', { class: 'pick-sub-head' }, [
        el('span', { class: 'ps-name' }, sub.name),
        subAll,
      ]),
    ]);
    sub.companies.forEach(c => block.appendChild(companyCheckRow(c)));
    wrap.appendChild(block);
  });

  return wrap;
}

function companyCheckRow(c) {
  if (!c.runnable) {
    return el('div', { class: 'pick-company is-unrunnable' }, [
      el('span', { class: 'check-box is-off is-disabled' }),
      el('span', { class: 'pc-name' }, c.display_name),
      el('span', { class: 'pc-note' }, 'not yet supported'),
    ]);
  }
  const on = PICK_SELECTED.has(c.key);
  const row = el('button', {
    class: 'pick-company' + (on ? ' is-on' : ''),
    onclick: () => { toggleKeys([c.key], !PICK_SELECTED.has(c.key)); },
  }, [
    el('span', { class: 'check-box' + (on ? ' is-on' : ' is-off') }, on ? checkGlyph() : null),
    el('span', { class: 'pc-name' }, c.display_name),
  ]);
  row.dataset.key = c.key;   // hook: refresh this row in place
  return row;
}

function selectAllControl(label, on, onToggle) {
  return el('button', { class: 'select-all' + (on ? ' is-on' : ''), onclick: onToggle }, [
    el('span', { class: 'check-box small' + (on ? ' is-on' : ' is-off') }, on ? checkGlyph() : null),
    el('span', {}, label),
  ]);
}

function isAllOn(keys) {
  return keys.length > 0 && keys.every(k => PICK_SELECTED.has(k));
}

/* Toggle selection AND repaint only what changed — the affected company rows,
   the sub-bucket/bucket "All" controls, and the run-bar count. NO full redraw
   and NO scroll, so ticking a checkbox keeps your place on the page. */
function toggleKeys(keys, turnOn) {
  keys.forEach(k => { if (turnOn) PICK_SELECTED.add(k); else PICK_SELECTED.delete(k); });
  keys.forEach(k => paintCompanyRow(k));
  paintAllControls();
  paintRunBarCount();
}

/* Set one company row's checkbox visual from PICK_SELECTED, in place. */
function paintCompanyRow(key) {
  const row = document.querySelector(`.pick-company[data-key="${cssEscape(key)}"]`);
  if (!row) return;
  const on = PICK_SELECTED.has(key);
  row.classList.toggle('is-on', on);
  const box = row.querySelector('.check-box');
  if (box) {
    box.classList.toggle('is-on', on);
    box.classList.toggle('is-off', !on);
    box.replaceChildren(on ? checkGlyph() : document.createTextNode(''));
  }
}

/* Refresh every "All" / "All in bucket" control from current selection. */
function paintAllControls() {
  document.querySelectorAll('.select-all[data-all-for]').forEach(ctrl => {
    const keys = (ctrl.dataset.allFor || '').split(',').filter(Boolean);
    const on = isAllOn(keys);
    ctrl.classList.toggle('is-on', on);
    const box = ctrl.querySelector('.check-box');
    if (box) {
      box.classList.toggle('is-on', on);
      box.classList.toggle('is-off', !on);
      box.replaceChildren(on ? checkGlyph() : document.createTextNode(''));
    }
  });
}

/* Update just the run-bar count + disabled state, in place. */
function paintRunBarCount() {
  const count = PICK_SELECTED.size;
  const num = document.querySelector('.pick-runbar .pick-count');
  if (num) num.textContent = String(count);
  const word = document.querySelector('.pick-runbar .pick-count-word');
  if (word) word.textContent = count === 1 ? ' company selected' : ' companies selected';
  const runBtn = document.querySelector('.pick-runbar .rb-run');
  if (runBtn) {
    if (count) { runBtn.removeAttribute('disabled'); runBtn.classList.remove('is-disabled'); }
    else { runBtn.setAttribute('disabled', ''); runBtn.classList.add('is-disabled'); }
  }
}

/* Minimal CSS.escape fallback (company keys are lowercase/dashes, but be safe). */
function cssEscape(s) {
  return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\\]]/g, '\\$&');
}

function checkGlyph() {
  return svgEl('0 0 16 16', [
    { t: 'path', d: 'M3.5 8.5l3 3 6-7', fill: 'none', stroke: 'currentColor',
      'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
  ], 'check-tick');
}

/* Run the ticked selection: same engine, fed company keys + the per-run filters
   from the panel (M). The filters apply to this run only; saved defaults are
   untouched unless the user pressed "Save as my defaults". */
async function runSelection() {
  const keys = Array.from(PICK_SELECTED);
  if (!keys.length) return;
  const label = keys.length === 1 ? '1 company' : `${keys.length} companies`;
  try {
    await api.post('/api/run/start', { keys, label, filters: _runFilterPayload() });
  } catch (e) { toast(e.message, true); return; }
  renderRunPanel(label);
  streamJobsRun();
}

/* The filter object sent with a run. Mirrors JOBS_FILTERS; the server coerces
   it (≤5 locations, valid mode) and treats an all-empty/rank object as "use
   saved defaults". */
function _runFilterPayload() {
  const f = JOBS_FILTERS || _cloneFilters({});
  return {
    keywords_ranked: f.keywords_ranked,
    keywords_mode: f.keywords_mode,
    locations_allowed: f.locations_allowed,
    departments_allowed: f.departments_allowed,
    departments_mode: f.departments_mode,
    experience_years_max: f.experience_years_max,
  };
}

/* Like streamRun, but on completion it lands in the Jobs two-pane view rather
   than the v1 report route. Reuses the same SSE event handling/progress bar. */
function streamJobsRun() {
  const source = new EventSource('/api/run/stream');
  const list = $('#runList');
  const rows = {};
  let total = 0, doneCount = 0;
  const setBar = () => {
    const pct = total ? Math.round((doneCount / total) * 100) : 0;
    $('#barFill').style.width = pct + '%';
    $('#barPct').textContent = pct + '%';
    $('#barText').textContent = `${doneCount} of ${total} checked`;
  };
  source.onmessage = (msg) => {
    let ev; try { ev = JSON.parse(msg.data); } catch { return; }
    switch (ev.kind) {
      case 'run_start':
        total = ev.total || 0;
        $('#runNote').textContent = ev.note || '';
        $('#barText').textContent = `0 of ${total} checked`;
        $('#barPct').textContent = '0%';
        break;
      case 'company_start': {
        const li = el('li', {}, [
          el('span', { class: 'tick doing' }, '○'),
          el('span', { class: 'r-name' }, ev.name),
          el('span', { class: 'r-delta', id: `d-${ev.key}` }, 'checking…'),
        ]);
        rows[ev.key] = li; list.appendChild(li);
        updateCompanyBar(ev.name, 'starting');
        break;
      }
      case 'company_stage':
        updateCompanyBar(ev.name, ev.stage);
        break;
      case 'company_done': {
        let li = rows[ev.key];
        if (!li) {
          // Re-attach replay: a company finished while we were away, so we never
          // saw its company_start. Build the row now so it still shows.
          li = el('li', {}, [
            el('span', { class: 'tick doing' }, '○'),
            el('span', { class: 'r-name' }, ev.name),
            el('span', { class: 'r-delta', id: `d-${ev.key}` }, ''),
          ]);
          rows[ev.key] = li; list.appendChild(li);
          doneCount++;  // count it once (live company_done counts via the else path)
        } else {
          doneCount++;
        }
        const tick = li.firstChild;
        const delta = $('#d-' + ev.key, li) || li.lastChild;
        if (ev.skipped) { tick.className = 'tick skipped'; tick.textContent = '–'; delta.textContent = 'not yet supported'; }
        else if (ev.ok) {
          tick.className = 'tick done'; tick.textContent = '✓';
          delta.replaceChildren(
            ev.new_count ? el('span', { class: 'new' }, `${ev.new_count} new`) : document.createTextNode('no change'),
            document.createTextNode(ev.removed_count ? ` · ${ev.removed_count} gone` : ''));
        } else { tick.className = 'tick failed'; tick.textContent = '!'; delta.textContent = 'couldn’t reach — skipped'; }
        setBar(); break;
      }
      case 'run_failed':
        source.close();
        $('#view').replaceChildren(
          el('button', { class: 'backlink', onclick: () => { RUN_RESULT = null; go('jobs'); } }, '← Jobs'),
          errorBox('That check couldn’t run.', ev.message));
        break;
      case 'result':
        LAST_RESULT = ev.result; RUN_RESULT = ev.result; break;
      case 'run_cancelled':
        $('#runNote').textContent =
          `Stopped — checked ${ev.checked} of ${ev.total}. Showing what we got.`;
        break;
      case 'end':
        source.close();
        $('#barFill').style.width = '100%'; $('#barPct').textContent = '100%';
        clearCompanyBar();
        // Refresh home (last-check changed) + saved set, then show two-pane.
        Promise.all([
          api.get('/api/home').then(h => { HOME = h; }).catch(() => {}),
          api.get('/api/jobs').then(j => { JOBS = j; }).catch(() => {}),
          refreshSaved(),
        ]).finally(() => {
          RUN_FOCUS = null; DEPT_VIEW = null; JOBS_TAB = 'check';
          setTimeout(() => { if (RUN_RESULT) drawJobs(); else go('jobs'); }, 300);
        });
        break;
      case 'idle': source.close(); go('jobs'); break;
    }
  };
  source.onerror = () => {
    if (!RUN_RESULT) { source.close(); toast('Lost contact with the check. Try again.', true); go('jobs'); }
  };
}

/* "Show last run" — load the saved report into the two-pane view. */
async function showLastRun() {
  try {
    const r = await api.get('/api/last-report');
    if (!r || !r.result) { toast('No saved run yet.'); return; }
    RUN_RESULT = r.result; RUN_FOCUS = null; DEPT_VIEW = null;
    await refreshSaved();
    drawJobs();
  } catch (e) { toast(e.message, true); }
}

/* ---- the two-pane results view --------------------------------------- */
/* Left 25%: the companies you ran. Right 75%: by default the new/removed roles
   across the run; click a company to swap the right pane to that company's FULL
   current list (from result.companies[].current), new roles highlighted. */
function twoPaneResults(result) {
  const ran = (result.companies || []);
  const counts = result.counts || {};

  // Left rail — companies in this run.
  const rail = el('div', { class: 'run-rail' }, [
    el('button', {
      class: 'rail-item rail-overview' + (RUN_FOCUS === null ? ' on' : ''),
      onclick: () => { RUN_FOCUS = null; DEPT_VIEW = null; drawJobs(true); },
    }, [
      el('span', { class: 'rail-name' }, 'New & removed'),
      el('span', { class: 'rail-meta' }, `${counts.total_new ?? 0} new · ${counts.total_removed ?? 0} gone`),
    ]),
    ...ran.map(c => {
      const newN = (c.new || []).length;
      const curN = (c.current || []).length;
      const metaTxt = c.skipped ? 'not yet supported'
        : !c.ok ? 'couldn’t reach'
        : `${curN} role${curN === 1 ? '' : 's'}` + (newN ? ` · ${newN} new` : '');
      return el('button', {
        class: 'rail-item' + (RUN_FOCUS === c.key ? ' on' : '') + (c.skipped || !c.ok ? ' is-quiet' : ''),
        onclick: (c.skipped || !c.ok) ? null : () => { RUN_FOCUS = c.key; DEPT_VIEW = null; drawJobs(true); },
      }, [
        el('span', { class: 'rail-name' }, c.name),
        el('span', { class: 'rail-meta' }, metaTxt),
      ]);
    }),
  ]);

  // Right pane — overview (new/removed) or a focused company's full list.
  const pane = RUN_FOCUS === null
    ? runOverviewPane(result)
    : companyRolesPane(ran.find(c => c.key === RUN_FOCUS));

  return el('div', { class: 'run-twopane' }, [
    el('div', { class: 'run-rail-wrap' }, [
      el('button', { class: 'backlink', onclick: () => { RUN_RESULT = null; RUN_FOCUS = null; DEPT_VIEW = null; drawJobs(true); } }, '← Back to Jobs'),
      rail,
    ]),
    el('div', { class: 'run-pane' }, [pane]),
  ]);
}

function runOverviewPane(result) {
  const counts = result.counts || {};
  const head = el('div', { class: 'pane-head' }, [
    el('h2', {}, result.bucket || 'Results'),
    el('div', { class: 'tally' }, [
      el('b', {}, `${counts.total_new ?? 0} new`),
      document.createTextNode(
        ` · ${counts.total_removed ?? 0} removed · ${counts.companies_checked ?? 0} checked` +
        (counts.companies_failed ? ` · ${counts.companies_failed} unreachable` : '') +
        (counts.companies_skipped ? ` · ${counts.companies_skipped} not yet supported` : '')),
    ]),
  ]);

  if ((counts.total_new ?? 0) === 0 && (counts.total_removed ?? 0) === 0) {
    return el('div', {}, [head, el('div', { class: 'empty' }, [
      el('h3', {}, 'Nothing moved'),
      el('p', {}, 'No new or removed roles since the last check. Click a company on the left to see everything it’s currently listing.'),
    ])]);
  }

  const blocks = [head];
  for (const c of result.companies || []) {
    if (c.skipped) continue;
    const newJobs = c.new || [];
    const removed = c.removed || [];
    if (!newJobs.length && !removed.length && c.ok) continue;
    const ctx = { key: c.key, name: c.name };
    const countLabel = c.baseline
      ? `baseline · ${newJobs.length} recorded`
      : `${newJobs.length} new` + (removed.length ? ` · ${removed.length} removed` : '');
    const block = el('div', { class: 'company-block' }, [
      el('h3', {}, [
        document.createTextNode(c.name),
        el('span', { class: 'c-count' }, c.ok ? countLabel : 'couldn’t reach'),
      ]),
    ]);
    newJobs.forEach(j => block.appendChild(jobRow(j, 'new', false, ctx)));
    removed.forEach(j => block.appendChild(jobRow(j, 'removed', false, ctx)));
    blocks.push(block);
  }
  return el('div', {}, blocks);
}

function companyRolesPane(c) {
  if (!c) return el('div', { class: 'empty' }, [el('h3', {}, 'Pick a company')]);
  const ctx = { key: c.key, name: c.name };
  const current = c.current || [];
  const newIds = new Set((c.new || []).map(j => String(j.id)));

  const head = el('div', { class: 'pane-head' }, [
    el('h2', {}, c.name),
    el('div', { class: 'tally' }, [
      el('b', {}, `${current.length} current role${current.length === 1 ? '' : 's'}`),
      document.createTextNode((c.new || []).length ? ` · ${(c.new || []).length} new this run` : ''),
    ]),
  ]);

  if (!current.length) {
    return el('div', {}, [head, el('div', { class: 'empty' }, [
      el('h3', {}, 'No current roles'),
      el('p', {}, c.ok ? 'This company isn’t listing any roles in your locations right now.' : 'This company couldn’t be reached on the last run.'),
    ])]);
  }

  // Departments actually hiring here — the discovery view, so you pick real
  // department names instead of guessing ("is Corporate hiring? Finance?").
  // Computed from this company's FULL current roles; click a chip to narrow the
  // VIEW to that department (transient — never touches the saved filter).
  const deptSummary = departmentSummary(current);

  // Transient view-filter: if a department chip is active, show only roles in it.
  // The chip set above is still computed from the full list so you can switch.
  const shown = DEPT_VIEW
    ? current.filter(j => (j.department || '').trim() === DEPT_VIEW)
    : current;

  // "showing: <dept> · clear" indicator, only when a view-filter is active.
  const viewNote = DEPT_VIEW ? el('div', { class: 'dept-view-note' }, [
    el('span', { class: 'dvn-label' }, [
      document.createTextNode('Showing '),
      el('b', {}, DEPT_VIEW),
      document.createTextNode(` · ${shown.length} role${shown.length === 1 ? '' : 's'}`),
    ]),
    el('button', { class: 'dvn-clear', onclick: () => { DEPT_VIEW = null; drawJobs(); } }, 'clear'),
  ]) : null;

  const block = el('div', { class: 'company-block' });
  shown.forEach(j => {
    const isNew = newIds.has(String(j.id));
    block.appendChild(jobRow(j, isNew ? 'new' : 'current', false, ctx));
  });
  return el('div', {}, [head, deptSummary, viewNote, block].filter(Boolean));
}

/* A compact "departments hiring" chip row from a set of roles: distinct
   non-blank departments with counts, biggest first. Clicking a chip narrows the
   VISIBLE roles to that department (a transient view filter) — it does NOT touch
   the saved/active filter set, and it does NOT re-run. Click the active chip
   again (or "clear") to show everything. */
function departmentSummary(roles) {
  const counts = {};
  for (const j of roles) {
    const d = (j.department || '').trim();
    if (!d) continue;
    counts[d] = (counts[d] || 0) + 1;
  }
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (!rows.length) return null;

  return el('div', { class: 'dept-summary' }, [
    el('div', { class: 'dept-summary-label' }, 'Departments hiring here:'),
    el('div', { class: 'dept-chip-row' }, rows.map(([name, n]) => {
      const active = DEPT_VIEW === name;
      return el('button', {
        class: 'dept-chip' + (active ? ' on' : ''),
        title: active ? `Showing only “${name}” — click to show all` : `Show only “${name}”`,
        onclick: () => toggleDepartmentView(name),
      }, [
        el('span', { class: 'dc-name' }, name),
        el('span', { class: 'dc-count' }, String(n)),
      ]);
    })),
  ]);
}

/* Toggle the transient department VIEW filter on the focused company's list.
   Click a chip → show only that department; click the active chip again →
   show all. Different chip → switch. Never mutates JOBS_FILTERS; never re-runs;
   no toast. Cleared on company change / fresh run elsewhere. (Locked S27.) */
function toggleDepartmentView(name) {
  DEPT_VIEW = (DEPT_VIEW === name) ? null : name;
  drawJobs();
}

/* ---- the star control ------------------------------------------------- */
function starControl(job, companyKey, companyName) {
  const key = _savedKey(companyKey, String(job.id));
  const on = SAVED_SET.has(key);
  const btn = el('button', {
    class: 'star' + (on ? ' is-on' : ''),
    title: on ? 'Saved — click to remove' : 'Save this role',
    'aria-label': on ? 'Remove from saved' : 'Save this role',
    onclick: async (e) => {
      e.preventDefault(); e.stopPropagation();
      await toggleSave(job, companyKey, companyName, btn);
    },
  }, [starGlyph(on)]);
  return btn;
}

async function toggleSave(job, companyKey, companyName, btn) {
  const key = _savedKey(companyKey, String(job.id));
  const wasOn = SAVED_SET.has(key);
  try {
    if (wasOn) {
      await api.post('/api/saved/remove', { company_key: companyKey, id: job.id });
      SAVED_SET.delete(key);
    } else {
      await api.post('/api/saved/add', {
        job, company_key: companyKey, company_name: companyName,
      });
      SAVED_SET.add(key);
    }
    btn.classList.toggle('is-on', !wasOn);
    btn.replaceChildren(starGlyph(!wasOn));
    btn.title = !wasOn ? 'Saved — click to remove' : 'Save this role';
  } catch (e) { toast(e.message, true); }
}

function starGlyph(filled) {
  return svgEl('0 0 20 20', [
    { t: 'path',
      d: 'M10 1.8l2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L2.2 7.5l5.4-.8z',
      fill: filled ? 'currentColor' : 'none', stroke: 'currentColor',
      'stroke-width': 1.4, 'stroke-linejoin': 'round' },
  ], 'star-glyph' + (filled ? ' filled' : ''));
}

/* ---- SAVED tab -------------------------------------------------------- */

function jobsSavedBody() {
  const wrap = el('div', { class: 'saved-wrap' });
  const active = SAVED_LIST.filter(s => !s.applied);

  if (!active.length) {
    wrap.appendChild(el('div', { class: 'empty' }, [
      el('h3', {}, 'No saved jobs yet'),
      el('p', {}, 'Star a role while reading a run and it lands here. From here you can open it, mark it applied, or remove it.'),
    ]));
    return wrap;
  }

  // Sort controls.
  wrap.appendChild(el('div', { class: 'saved-sort' }, [
    el('span', { class: 'ss-label' }, 'Sort'),
    sortBtn('date', 'Date added'),
    sortBtn('company', 'Company'),
    sortBtn('alpha', 'A–Z'),
  ]));

  const sorted = sortSaved(active);
  const list = el('div', { class: 'company-block saved-list' });
  sorted.forEach(s => list.appendChild(savedRow(s)));
  wrap.appendChild(list);
  return wrap;
}

function sortBtn(key, label) {
  return el('button', {
    class: 'seg-btn' + (SAVED_SORT === key ? ' on' : ''),
    onclick: () => { SAVED_SORT = key; drawJobs(); },
  }, label);
}

function sortSaved(items) {
  const a = items.slice();
  if (SAVED_SORT === 'company') {
    a.sort((x, y) => (x.company_name || '').localeCompare(y.company_name || '') ||
                     (x.title || '').localeCompare(y.title || ''));
  } else if (SAVED_SORT === 'alpha') {
    a.sort((x, y) => (x.title || '').localeCompare(y.title || ''));
  } else { // date — newest first
    a.sort((x, y) => (y.saved_on || '').localeCompare(x.saved_on || ''));
  }
  return a;
}

function savedRow(s) {
  const sub = [];
  sub.push(el('span', { class: 'j-company' }, s.company_name));
  if (s.location) sub.push(el('span', {}, s.location));
  if (s.department) sub.push(el('span', {}, s.department));
  const added = fmtLongDate(s.saved_on);
  if (added) sub.push(el('span', { class: 'muted' }, `saved ${added}`));

  const titleNode = s.url
    ? el('a', { href: s.url, target: '_blank', rel: 'noopener' }, s.title || '(untitled role)')
    : document.createTextNode(s.title || '(untitled role)');

  const tags = flagTags(s);  // M.5 — re-flagged on read by the server

  return el('div', { class: 'job is-current saved-row' }, [
    el('span', { class: 'marker' }),
    el('div', { class: 'j-main' }, [
      el('div', { class: 'j-title' }, [titleNode, ...tags]),
      el('div', { class: 'j-sub' }, sub),
    ]),
    el('div', { class: 'saved-actions' }, [
      el('button', { class: 'btn ghost small', onclick: () => markApplied(s) }, 'Applied'),
      el('button', { class: 'btn ghost small danger', onclick: () => removeSaved(s) }, 'Remove'),
    ]),
  ]);
}

async function markApplied(s) {
  try {
    await api.post('/api/saved/applied', { company_key: s.company_key, id: s.id });
    SAVED_SET.delete(_savedKey(s.company_key, s.id));
    toast('Moved into your application tracker.');
    await refreshSaved();
    drawJobs();
  } catch (e) { toast(e.message, true); }
}

async function removeSaved(s) {
  try {
    await api.post('/api/saved/remove', { company_key: s.company_key, id: s.id });
    SAVED_SET.delete(_savedKey(s.company_key, s.id));
    await refreshSaved();
    drawJobs();
  } catch (e) { toast(e.message, true); }
}


/* ---- boot -------------------------------------------------------------- */
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { const v = $('.modal-veil'); if (v) v.remove(); }
});

// Hover/focus reveal for the Home banner, set up once.
wireBannerReveal();

// (H) Repaint live if the OS appearance flips (e.g. at dusk) while on "system".
wireSystemModeListener();

// Route on hash change (covers refresh, back/forward, and go()).
window.addEventListener('hashchange', render);

// First paint: theme as early as possible to avoid a flash, then load the saved
// appearance preference and the route. applyTheme() runs again inside render()
// once HOME is known; calling it here first means the very first frame is themed
// (dormant default) rather than unstyled.
applyTheme();
loadColorMode().then(() => {
  applyTheme();   // re-apply with the user's saved light/dark/system choice
  render();
});
