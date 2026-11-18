/* github-copilot-usage dashboard.
 *
 * Polls /api/summary every 30 s (plus /api/sessions and the optional
 * /api/billing). Charts are Chart.js (vendored — no CDN so it works behind
 * corporate proxies).
 *
 * Model colors (provider family hue, one lightness step per rank) and their
 * validation live in models.js. Stacked segments carry a 2px surface gap as
 * the secondary encoding.
 */

import { buildReadoutText } from './_vendored/page-foot/page-foot.js';
import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';
import { renderHeadStatus } from './_vendored/home-head/home-head.js';
import { showToast as showFleetToast } from './_vendored/toast/toast.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import { actionRowHtml, esc, splitMeta } from './rows.js';
import { initRequests } from './requests.js';
import { flagChips, initInsights, refreshInsights, renderDiagnostics } from './insights.js';
import { initFeatures } from './features.js';
import { initTailnet } from './tailnet.js';
import { createModelColors, initModelSettings, summarizeModelSettings } from './models.js';
import { createCharts } from './charts.js';
import { initSections } from './sections.js';
import { DEFAULT_PERIOD, DEFAULT_SORT, initFilters } from './filters.js';
import { initSessionName } from './session-name.js';
import { fetchTrend, initTimeline, render as renderTimeline } from './timeline.js';
import { cacheLine, fmtNum, fmtTok, tokBreakdown, unknownTokensNote } from './tokens.js';

// Local-only usage log (views, taps, device class); same-origin, no content recorded. It runs only
// while its feature block is on, and switching the block starts or stops it with no reload.
const NO_USAGE = { trackView() {}, stop() {} };
let usage = NO_USAGE;
function syncUsageLog() {
  const on = features.isOn('usage_log');
  if (on && usage === NO_USAGE) {
    usage = initUsageLog();
  } else if (!on && usage !== NO_USAGE) {
    usage.stop();
    usage = NO_USAGE;
  }
}

const POLL_MS = 30_000;
// Billing changes server-side at most every 5 min (src/billing.py caches today),
// so the 30 s refresh need not re-ask each time; 60 s is a stated guess, not a
// measurement, and is deliberately shorter than that cache.
const BILLING_POLL_MS = 60_000;
const SESSIONS_PREVIEW = 12;

const state = {
  period: DEFAULT_PERIOD,
  summary: null,
  sessions: [],
  sessionsSort: DEFAULT_SORT,
  sessionsExpanded: false,
  trendOffset: 0,       // how many trend windows back the charts are stepped
  trend: null,          // the older window fetched for that offset; null while the latest is open
  returnPeriod: null,   // the period a focused date range goes back to
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

$('theme-toggle').addEventListener('click', () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('copilot-usage.theme', next);
  if (state.summary) {   // re-resolve palette + axis colors, and the table swatches that share them
    renderCharts();
    renderModelTable(state.summary.by_model || []);
  }
});

// ---------------------------------------------------------------------------
// Text size — the zoom-lock escape (the pre-paint stamp lives in index.html)
// ---------------------------------------------------------------------------

const TEXT_SIZES = ['small', 'default', 'large'];

function paintTextSize() {
  const size = document.documentElement.dataset.textsize || 'default';
  document.querySelectorAll('#set-textsize [data-textsize]').forEach((btn) => {
    const on = btn.dataset.textsize === size;
    btn.setAttribute('aria-pressed', String(on));
  });
}

$('set-textsize').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-textsize]');
  if (!btn || !TEXT_SIZES.includes(btn.dataset.textsize)) return;
  document.documentElement.dataset.textsize = btn.dataset.textsize;
  try { localStorage.setItem('copilot-usage.textsize', btn.dataset.textsize); } catch { /* applies for this view only */ }
  paintTextSize();
  toast('Text size: ' + btn.textContent.trim().toLowerCase());
});
paintTextSize();

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtCredits(n) {
  if (n === undefined || n === null) return '—';
  n = Number(n);
  return n.toLocaleString(undefined, { maximumFractionDigits: n >= 100 ? 0 : 2 });
}

function fmtUsd(n) {
  if (n === undefined || n === null) return '';
  return '$' + Number(n).toFixed(2);
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return ''; }
}

function fmtDay(iso) {
  try {
    return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch { return ''; }
}

// A status line can name an environment variable (the server's own reason text);
// each name is drawn as inline code, never as ordinary prose.
const ENV_NAME = /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g;
function setNote(el, text) {
  el.innerHTML = esc(text).replace(ENV_NAME, '<code>$&</code>');
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// What the server last confirmed; a failed save puts the field back to this.
let settings = {
  monthly_credits: 0, cycle_reset_day: 1, extra_roots: [],
  model_aliases: {}, model_colors: {},
};

const colors = createModelColors(() => settings.model_colors || {}, currentTheme);

// ---------------------------------------------------------------------------
// Fetch + poll
// ---------------------------------------------------------------------------

async function jsonApi(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  return resp.json();
}

// ---------------------------------------------------------------------------
// Page header: the tab's title, the live context line and the freshness readout
// ---------------------------------------------------------------------------

const TAB_ICONS = { overview: 'i-gauge', trends: 'i-activity', breakdown: 'i-layout-grid', sessions: 'i-messages-square' };
const PERIOD_NAMES = { today: 'Today', week: 'This week', month: 'This month', cycle: 'This billing cycle', all: 'All time' };

function paintHeader(tab) {
  const button = document.querySelector(`.tabs .tab[data-tab="${tab}"]`);
  $('head-title').textContent = button ? button.textContent.trim() : '';
  $('head-icon').setAttribute('href', '#' + (TAB_ICONS[tab] || 'i-gauge'));
}

// The context line names the period on show; a failed read keeps the last figures and says so.
function paintHeadStatus(failed) {
  const fallback = PERIOD_NAMES[state.period] || $('focus-label').textContent;
  renderHeadStatus($('head-status'), failed ? [{ text: 'Live data unavailable', tone: 'danger' }] : [], fallback);
}

function paintFreshness(at) {
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  $('freshness').textContent = time;
  $('menu-btn').setAttribute('aria-label', 'Updated ' + time + '. Refresh options');
}

// Loads are numbered as they start. A response is drawn only while no newer load has been drawn,
// so a slow poll never paints over a newer visibility, period or refresh-menu load.
let loadSeq = 0;
let drawnSeq = 0;
let loadsInFlight = 0;
let lastLoadedAt = 0;   // when a load last succeeded; 0 until the first one lands

async function refresh() {
  const period = encodeURIComponent(state.period);
  const offset = state.trendOffset;
  const seq = ++loadSeq;
  loadsInFlight += 1;
  try {
    const [summary, sessions, trend] = await Promise.all([
      jsonApi('/api/summary?period=' + period),
      jsonApi('/api/sessions?period=' + period),
      fetchTrend(state),
    ]);
    if (seq > drawnSeq) {
      drawnSeq = seq;
      state.summary = summary;
      state.sessions = sessions.sessions || [];
      if (offset === state.trendOffset) state.trend = trend;
      render();
      lastLoadedAt = Date.now();
      paintFreshness(new Date());
      paintHeadStatus(false);
    }
  } catch (exc) {
    if (seq > drawnSeq) paintHeadStatus(true);   // a failure of a superseded load says nothing new
    console.error(exc);
  } finally {
    loadsInFlight -= 1;
  }
  refreshInsights();
  fetchBilling().catch(() => {});
}

// Hidden tabs have their timers throttled or suspended, so the figures can be older than one poll
// on return. A tab that comes back to a stale view loads once; a load already running is that load.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (loadsInFlight > 0 || Date.now() - lastLoadedAt <= POLL_MS) return;
  refresh();
});

let _billingFetched = 0;
async function fetchBilling() {
  if (!features.isOn('billing_card')) return;
  if (Date.now() - _billingFetched < BILLING_POLL_MS) return;
  _billingFetched = Date.now();
  let body;
  try {
    body = await jsonApi('/api/billing');
  } catch (exc) {
    // The card could not tell: say so, and let the next 30 s refresh retry.
    _billingFetched = 0;
    $('billing-wrap').hidden = true;
    $('billing-asof').textContent = '';
    setNote($('billing-note'), 'Could not load billing data from the dashboard server.');
    console.error(exc);
    return;
  }
  renderBilling(body);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function render() {
  const s = state.summary;
  if (!s) return;
  colors.setMeta(s.model_meta);
  colors.seed(s.by_model || []);
  renderTiles(s);
  renderBudget(s.budget, s.coverage);
  renderCharts();
  renderTimeline(state);
  renderMix(s.prompt_mix || [], $('mix-card'), $('mix-bars'));
  renderModelTable(s.by_model || []);
  renderProjectTable(s.by_project || [], s.by_mode || []);
  renderSessions();
  renderSources(s.sources || []);
  $('export-link').href = '/api/export.csv?period=' + encodeURIComponent(state.period);
  // An empty period offers the way forward unless it already spans every record.
  document.querySelectorAll('[data-show-all]').forEach((b) => { b.hidden = state.period === 'all'; });
}

const TILE_VALUES = ['t-credits', 't-requests', 't-in', 't-out'];
const TILE_NOTES = ['t-usd', 't-avg', 't-in-sub', 't-out-sub'];

// A period the telemetry start date does not reach has no figures, which is not the same as zero.
function blankTiles() {
  TILE_VALUES.forEach((id) => { $(id).textContent = '—'; });
  TILE_NOTES.forEach((id) => { $(id).textContent = ''; });
  $('t-in').title = '';
  $('d-credits').hidden = true;
  $('d-requests').hidden = true;
}

function renderTiles(s) {
  if (s.coverage && s.coverage.state === 'none') { blankTiles(); return; }
  const t = s.totals || {};
  $('t-credits').textContent = fmtCredits(t.credits);
  $('t-usd').textContent = t.credits ? '≈ ' + fmtUsd(t.usd) : '';
  $('t-requests').textContent = fmtNum(t.requests);
  $('t-avg').textContent = [
    t.billed_requests ? fmtCredits(t.avg_credits_per_request) + ' cr/req' : '',
    t.estimated_requests ? fmtNum(t.estimated_requests) + ' estimated' : '',
  ].filter(Boolean).join(' · ');
  $('t-in').textContent = fmtTok(t.prompt_tokens);
  $('t-in').title = tokBreakdown(t);
  $('t-out').textContent = fmtTok(t.completion_tokens);
  $('t-in-sub').textContent = [
    cacheLine(t) || (t.requests && t.prompt_tokens != null
      ? '~' + fmtTok(t.prompt_tokens / Math.max(t.requests - (t.unknown_token_requests || 0), 1)) + ' per request' : ''),
    unknownTokensNote(t),
  ].filter(Boolean).join(' · ');
  $('t-out-sub').textContent = [
    t.errors ? t.errors + ' failed req (' + fmtCredits(t.error_credits) + ' cr)' : '',
    t.tokens_per_credit == null ? '' : fmtTok(t.tokens_per_credit) + ' effective tok/credit',
  ].filter(Boolean).join(' · ');

  renderDelta($('d-credits'), t.credits, s.prev_totals && s.prev_totals.credits);
  renderDelta($('d-requests'), t.requests, s.prev_totals && s.prev_totals.requests);
}

function renderDelta(el, curr, prev) {
  if (!el) return;
  if (prev === undefined || prev === null || state.period === 'all' || state.period === 'cycle') {
    el.hidden = true;
    return;
  }
  curr = Number(curr) || 0; prev = Number(prev) || 0;
  if (prev === 0) {
    el.hidden = !(curr > 0);
    el.className = 'delta up';
    el.textContent = 'new';
    return;
  }
  const pct = Math.round(((curr - prev) / prev) * 100);
  el.hidden = false;
  el.className = pct > 0 ? 'delta up' : pct < 0 ? 'delta down' : 'delta';
  el.textContent = (pct > 0 ? '+' : '') + pct + '% vs prev';
}

// ---- budget ----

function budgetNote(b, startDate) {
  const note = $('budget-note');
  if (!note.dataset.base) note.dataset.base = note.textContent;
  const from = startDate ? fmtDay(startDate + 'T00:00:00') : '';
  if (b.coverage === 'none') return 'No telemetry counted this cycle: it ends before your telemetry start date, ' + from + '.';
  if (b.coverage === 'partial') {
    return note.dataset.base + ' Partial coverage: counting from ' + from + '; earlier requests in this cycle are left out.';
  }
  return note.dataset.base;
}

const RISK_LABEL = { attention: 'Attention', warning: 'Warning', critical: 'Critical' };

function renderBudget(b, coverage) {
  if (!b) { $('budget-card').hidden = true; return; }
  $('budget-card').hidden = false;
  $('budget-note').textContent = budgetNote(b, coverage && coverage.start_date);
  $('budget-range').textContent = fmtDay(b.cycle_start) + ' – ' + fmtDay(b.cycle_end)
    + ' · day ' + b.days_elapsed + '/' + b.days_total;

  const pct = Math.min(100, b.used_pct || 0);
  const fill = $('budget-fill');
  fill.style.width = pct + '%';
  const risk = b.risk || 'none';
  fill.className = 'budget-fill' + (risk === 'none' ? '' : ' ' + risk);
  // The level is spelled out as text beside the used figure, never left to the meter's colour.
  const chip = $('budget-risk');
  chip.hidden = risk === 'none';
  chip.className = 'sev sev-' + risk;
  chip.textContent = RISK_LABEL[risk] || '';

  const marker = $('budget-today-marker');
  marker.style.left = Math.min(100, (b.days_elapsed / b.days_total) * 100) + '%';

  $('budget-used').textContent =
    fmtCredits(b.used_credits) + ' / ' + fmtNum(b.allowance_credits)
    + ' credits (' + (b.used_pct ?? 0) + '%)';
  $('budget-projection').textContent =
    'projected ' + fmtCredits(b.projected_credits) + ' (' + (b.projected_pct ?? 0) + '%) by cycle end';
}

// ---- charts ----

const renderCharts = createCharts({ $, state, colors, currentTheme, cssVar, fmtCredits, fmtTok });

// ---- prompt mix ----

function renderMix(mix, card, container) {
  if (!mix.length) { card.hidden = true; return; }
  card.hidden = false;
  // Each row is a button: it opens the drill-down for that label (see insights.js).
  container.innerHTML = mix.map((row) => `
    <button data-track="context-open-label" class="mix-row mix-pick" type="button" data-label="${esc(row.label)}" aria-haspopup="dialog">
      <span class="mix-top">
        <span>${esc(row.label)}</span>
        <span class="pct">${row.pct}% · ~${fmtTok(row.est_tokens)} tok</span>
      </span>
      <span class="mix-track"><span class="mix-fill" style="width:${Math.min(100, row.pct)}%"></span></span>
    </button>`).join('');
}

// ---- tables ----

// A grouped row's hover names every spelling it holds; a single spelling is just the name.
function modelTitle(r) {
  const raw = r.raw_models || [];
  return raw.length > 1 ? r.model + ' — also logged as: ' + raw.filter((n) => n !== r.model).join(', ') : r.model;
}

function renderModelTable(rows) {
  const tbody = $('model-table').querySelector('tbody');
  $('model-empty').hidden = rows.length > 0;
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td class="td-trunc" title="${esc(modelTitle(r))}"><span class="model-dot" style="background:${colors.colorOf(r.model)}"></span>${esc(r.model)}</td>
      <td class="num">${fmtNum(r.requests)}</td>
      <td class="num">${fmtCredits(r.credits)}</td>
      <td class="num">${r.credits_share_pct}%</td>
      <td class="num" title="${esc(tokBreakdown(r))}">${fmtTok(r.prompt_tokens)} / ${fmtTok(r.completion_tokens)}</td>
    </tr>`).join('');
}

function renderProjectTable(rows, byMode) {
  const tbody = $('project-table').querySelector('tbody');
  $('project-empty').hidden = rows.length > 0;
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td class="td-trunc" title="${esc(r.project_path || r.project)}">${esc(r.project)}</td>
      <td class="num">${fmtNum(r.requests)}</td>
      <td class="num">${fmtCredits(r.credits)}</td>
      <td class="num" title="${esc(tokBreakdown(r))}">${fmtTok(r.prompt_tokens)} / ${fmtTok(r.completion_tokens)}</td>
    </tr>`).join('');

  $('mode-chips').textContent = byMode
    .filter((m) => m.mode && m.mode !== 'unknown')
    .map((m) => m.mode + ' ' + fmtNum(m.requests))
    .join(' · ');
}

// ---- sessions ----

function sortedSessions() {
  const rows = [...state.sessions];
  if (state.sessionsSort === 'recent') {
    rows.sort((a, b) => (b.last_ts || '').localeCompare(a.last_ts || ''));
  }
  return rows;
}

// "9:42 PM–10:03 PM", or one time when the session began and ended in the same minute.
function sessionSpan(s) {
  const first = fmtTime(s.first_ts);
  const last = fmtTime(s.last_ts);
  return first === last ? first : first + '–' + last;
}

function renderSessions() {
  const rows = sortedSessions();
  $('sessions-count').textContent = rows.length;
  $('sessions-empty').hidden = rows.length > 0;
  const shown = state.sessionsExpanded ? rows : rows.slice(0, SESSIONS_PREVIEW);
  $('sessions-more').hidden = rows.length <= SESSIONS_PREVIEW;
  $('sessions-more').textContent = state.sessionsExpanded
    ? 'Show fewer' : 'Show all ' + rows.length;

  $('session-list').innerHTML = shown.map((s) => actionRowHtml({
    track: 'session-open',
    data: { sid: s.session_id },
    title: s.title,
    // Most telling words first: a phone width cuts the line from the right.
    meta: splitMeta(
      s.project + ' · ' + fmtDay(s.last_ts) + ' ' + sessionSpan(s) + ' · ' + fmtNum(s.requests) + ' req · ' + s.ide
        + (s.estimated_requests ? ' · estimated' : ''),
      fmtCredits(s.credits) + ' cr',
      flagChips(s.flags),
    ),
  })).join('');
}

$('sessions-more').addEventListener('click', () => {
  state.sessionsExpanded = !state.sessionsExpanded;
  renderSessions();
});

// The rows are real buttons, so Enter and Space already arrive as a click.
$('session-list').addEventListener('click', (e) => {
  const row = e.target.closest('button[data-sid]');
  if (row) openSession(row.dataset.sid);
});

// A request's attached files, tools and attachments (names only) sit in a row under it, opened
// by a button so the table keeps its width on a phone.
function contextToggle(r, i) {
  const n = (r.context_items || []).length;
  if (!n) return '';
  return `<button data-track="session-context-toggle" class="ctx-toggle" type="button" data-ctx="${i}" aria-expanded="false" aria-controls="sd-ctx-${i}">${fmtNum(n)} in context</button> `;
}

function contextRow(r, i) {
  const items = r.context_items || [];
  if (!items.length) return '';
  return `<tr class="ctx-row" id="sd-ctx-${i}" hidden><td colspan="8"><ul class="ctx-chips">${
    items.map((name) => `<li title="${esc(name)}">${esc(name)}</li>`).join('')}</ul></td></tr>`;
}

$('sd-table').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-ctx]');
  if (!btn) return;
  const open = btn.getAttribute('aria-expanded') !== 'true';
  btn.setAttribute('aria-expanded', String(open));
  $('sd-ctx-' + btn.dataset.ctx).hidden = !open;
});

async function openSession(sid) {
  let detail;
  try {
    detail = await jsonApi('/api/sessions/' + encodeURIComponent(sid));
  } catch { return; }
  if (detail.error) return;

  showSessionName(sid, detail);
  $('sd-title').textContent = detail.title || sid;
  $('sd-meta').textContent =
    detail.project + ' · ' + detail.ide
    + ' · ' + fmtDay(detail.first_ts) + ' ' + fmtTime(detail.first_ts) + ' – ' + fmtTime(detail.last_ts)
    + ' · ' + fmtNum(detail.requests) + ' requests · '
    + fmtCredits(detail.credits) + ' credits (≈ ' + fmtUsd(detail.usd) + ')'
    + (cacheLine(detail) ? ' · ' + cacheLine(detail) : '')
    + (unknownTokensNote(detail) ? ' · ' + unknownTokensNote(detail) : '')
    + (detail.estimated_requests
      ? ' · ' + fmtNum(detail.estimated_requests) + ' estimated (allocated from session totals, marked ~)' : '');

  renderDiagnostics($('sd-diagnostics'), detail.diagnostics);

  const mix = detail.prompt_mix || [];
  $('sd-mix').innerHTML = mix.length ? mix.map((row) => `
    <div class="mix-row">
      <div class="mix-top"><span>${esc(row.label)}</span><span class="pct">${row.pct}% · ~${fmtTok(row.est_tokens)} tok</span></div>
      <div class="mix-track"><div class="mix-fill" style="width:${Math.min(100, row.pct)}%"></div></div>
    </div>`).join('') : '';

  const tbody = $('sd-table').querySelector('tbody');
  tbody.innerHTML = (detail.requests_detail || []).map((r, i) => {
    const est = r.usage_estimated ? '~' : '';
    const tok = (n) => (n == null ? '—' : est + fmtTok(n));
    const rowTitle = r.usage_source === 'unavailable' ? 'The Copilot CLI recorded no token counts for this request'
      : r.usage_estimated ? 'Estimated: allocated from session totals (' + r.usage_source + ')' : '';
    return `
    <tr${r.error ? ' style="opacity:0.6"' : ''}${rowTitle ? ' title="' + esc(rowTitle) + '"' : ''}>
      <td class="num">${fmtTime(r.ts)}</td>
      <td class="td-trunc" title="${esc(r.message)}">${contextToggle(r, i)}${esc(r.message) || '<span class="muted">—</span>'}${r.error ? ' <svg class="inline-glyph glyph-warn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="errored"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>' : ''}</td>
      <td>${esc(r.mode)}</td>
      <td class="td-trunc" title="${esc(r.model)}">${esc(r.model)}</td>
      <td class="num" title="${esc(tokBreakdown(r))}">${tok(r.prompt_tokens)}</td>
      <td class="num">${tok(r.completion_tokens)}</td>
      <td class="num">${r.credits === null ? '—' : est + fmtCredits(r.credits)}</td>
      <td class="num">${r.elapsed_ms === null ? '—' : (r.elapsed_ms / 1000).toFixed(1) + 's'}</td>
    </tr>${contextRow(r, i)}`;
  }).join('');

  if (!$('session-dialog').open) $('session-dialog').showModal();
}

$('sd-close').addEventListener('click', () => $('session-dialog').close());

// ---- billing ----

function renderBilling(body) {
  const rows = body.daily || [];
  const missing = (body.failed_days || []).length;
  const tbody = $('billing-table').querySelector('tbody');
  $('billing-asof').textContent = body.as_of ? 'as of ' + fmtTime(body.as_of) : '';
  // The server decides the mode (src/billing_state.py); this only presents it. A card that
  // is not ready or partial shows the server's reason, never the empty-data sentence.
  const missingNote = missing
    ? ' ' + missing + (missing === 1 ? ' day' : ' days') + ' could not be fetched.'
    : '';
  if (!rows.length) {
    $('billing-wrap').hidden = true;
    setNote($('billing-note'), body.available
      ? 'No spend recorded in this window.' + missingNote
      : (body.reason || 'Not available.'));
    return;
  }
  $('billing-wrap').hidden = false;
  const total = rows.reduce((acc, r) => acc + (Number(r.credits) || 0), 0);
  const totalUsd = rows.reduce((acc, r) => acc + (Number(r.usd) || 0), 0);
  setNote($('billing-note'),
    'Account-wide official spend (all devices), last 14 days: '
    + fmtCredits(total) + ' credits ≈ ' + fmtUsd(totalUsd) + '.' + missingNote);
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td>${esc(r.date)}</td>
      <td class="td-trunc">${esc(r.model)}</td>
      <td class="num">${fmtCredits(r.credits)}</td>
      <td class="num">${fmtUsd(r.usd)}</td>
    </tr>`).join('');
}

// ---- sources ----

function renderSources(sources) {
  $('source-list').innerHTML = sources.map((s) => `
    <li>
      <span class="source-ide">${esc(s.ide)}</span>
      <span class="badge">${esc(s.kind)}</span>
      <span class="muted">${fmtNum(s.session_files)} session file${s.session_files === 1 ? '' : 's'} · ${fmtNum(s.requests)} requests${s.last_activity ? ' · last ' + fmtDay(s.last_activity) + ' ' + fmtTime(s.last_activity) : ''}</span>
      <span class="source-path">${esc(s.path)}</span>
      ${s.usage_note ? `<span class="muted">${esc(s.usage_note)}</span>` : ''}
      ${s.duplicates_ignored > 0 ? `<span class="muted">${fmtNum(s.duplicates_ignored)} duplicate request${s.duplicates_ignored === 1 ? '' : 's'} ignored: the same chat log is also read from another folder.</span>` : ''}
    </li>`).join('') || '<li><span class="muted">No Copilot data locations found.</span></li>';
}

// ---------------------------------------------------------------------------
// Period switching
// ---------------------------------------------------------------------------

// Also the way a tapped chart bucket focuses the dashboard: the period is then a date range, which
// lights no button of the segmented control.
function setPeriod(period) {
  state.period = period;
  state.sessionsExpanded = false;
  state.trendOffset = 0;
  state.trend = null;
  // The page's period control and the Filters & sort sheet's copy always agree.
  document.querySelectorAll('#period-seg button, #filters-period button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.period === period)));
  filters.paint();
  refresh();
}

['period-seg', 'filters-period'].forEach((id) => $(id).addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-period]');
  if (!btn || btn.dataset.period === state.period) return;
  state.returnPeriod = null;
  setPeriod(btn.dataset.period);
}));

document.querySelectorAll('[data-show-all]').forEach((btn) => btn.addEventListener('click', () => {
  state.returnPeriod = null;
  setPeriod('all');
}));

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

// The toast element is a manual popover, so it paints in the top layer above an open modal
// dialog. Each call re-stacks it on top: a popover shown before a dialog opened sits under it.
// Where popovers are missing the vendored toast still shows by its own `hidden` state.
function toast(message, kind) {
  const el = showFleetToast(message, kind);
  try {
    if (el.matches(':popover-open')) el.hidePopover();
    el.showPopover();
  } catch { /* shown without the top layer */ }
}

// Persist one setting. Resolves to the server's config, or null (and an error
// toast) when the write failed, so the caller can put the control back.
async function saveSetting(key, value, message) {
  try {
    const saved = await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: value }),
    });
    settings = { ...settings, ...saved };
    toast(message);
    refresh();
    $('set-models-value').textContent = summarizeModelSettings(settings);
    return saved;
  } catch (exc) {
    console.error(exc);
    toast('Could not save that setting. Try again.', 'error');
    return null;
  }
}

function rootsLabel(roots) {
  if (!roots.length) return 'None';
  return roots.length === 1 ? '1 path' : roots.length + ' paths';
}

function paintSettings(cfg) {
  settings = cfg;
  $('set-allowance').value = cfg.monthly_credits;
  $('set-reset').value = cfg.cycle_reset_day;
  $('set-start').value = cfg.telemetry_start_date || '';
  syncStartClear();
  $('set-roots-value').textContent = rootsLabel(cfg.extra_roots || []);
  $('set-models-value').textContent = summarizeModelSettings(cfg);
  const patSet = !!cfg.billing_pat_configured;
  const pat = $('set-pat-value');
  pat.textContent = patSet ? 'Configured' : 'Not set';
  if (patSet) delete pat.dataset.tone; else pat.dataset.tone = 'attention';
  // Static copy only (no user data), so innerHTML is safe and lets the env var wear the code chip.
  $('set-pat-note').innerHTML = 'Add <code>GITHUB_COPILOT_BILLING_PAT</code> to <code>.env</code> for the official billing card.';
  $('set-pat-note').hidden = patSet;
  const alertReason = cfg.pace_alert_reason;
  const alertValue = $('set-alert-value');
  alertValue.textContent = alertReason ? 'Off' : 'On';
  if (alertReason) alertValue.dataset.tone = 'attention'; else delete alertValue.dataset.tone;
  setNote($('set-alert-note'), alertReason
    ? 'Off — ' + alertReason + ', then restart. One message per cycle when the projection reaches your allowance.'
    : 'One Telegram message per cycle when the projection reaches your allowance.');
  $('alert-test').hidden = !!alertReason;
}

$('settings-btn').addEventListener('click', async () => {
  try {
    paintSettings(await jsonApi('/api/config'));
    await features.load();
  } catch { /* open anyway with stale values */ }
  $('settings-dialog').showModal();
});

$('settings-close').addEventListener('click', () => $('settings-dialog').close());

// A number saves when its field loses focus (the change event); a value out of
// range snaps to the nearest valid one, an empty or non-numeric one is put back.
function bindNumberSetting(id, key, min, max, label) {
  const el = $(id);
  el.addEventListener('change', async () => {
    const n = el.value.trim() === '' ? NaN : Number(el.value);
    if (!Number.isFinite(n)) { el.value = settings[key]; return; }
    const value = Math.min(max, Math.max(min, Math.round(n)));
    el.value = value;
    if (value === settings[key]) return;
    if (!(await saveSetting(key, value, label + ' saved'))) el.value = settings[key];
  });
}
bindNumberSetting('set-allowance', 'monthly_credits', 0, 1_000_000, 'Allowance');
bindNumberSetting('set-reset', 'cycle_reset_day', 1, 28, 'Reset day');

// The "Count every record" row only exists while a start date is set.
function syncStartClear() {
  $('set-start-clear').hidden = !$('set-start').value;
}

// An empty value counts every record again; the server refuses a date it cannot read.
$('set-start').addEventListener('change', async () => {
  const el = $('set-start');
  const before = settings.telemetry_start_date || '';
  if (el.value === before) return;
  const saved = await saveSetting('telemetry_start_date', el.value,
    el.value ? 'Counting from ' + fmtDay(el.value + 'T00:00:00') : 'Counting every record');
  if (!saved) el.value = before;
  syncStartClear();
});
$('set-start-clear').addEventListener('click', () => {
  $('set-start').value = '';
  $('set-start').dispatchEvent(new Event('change'));
});

$('roots-open').addEventListener('click', () => {
  $('set-roots').value = (settings.extra_roots || []).join('\n');
  $('roots-dialog').showModal();
});

$('roots-close').addEventListener('click', () => $('roots-dialog').close());
$('roots-done').addEventListener('click', () => $('roots-dialog').close());

// Saves on blur and again when the sheet closes (Esc closes it without a blur);
// the second call finds nothing changed and sends nothing.
async function saveRoots() {
  const roots = $('set-roots').value.split('\n').map((l) => l.trim()).filter(Boolean);
  const before = settings.extra_roots || [];
  if (roots.join('\n') === before.join('\n')) return;
  settings = { ...settings, extra_roots: roots };
  if (await saveSetting('extra_roots', roots, 'Storage roots saved')) {
    $('set-roots-value').textContent = rootsLabel(roots);
  } else {
    settings = { ...settings, extra_roots: before };
    $('set-roots').value = before.join('\n');
  }
}
$('set-roots').addEventListener('change', saveRoots);
$('roots-dialog').addEventListener('close', saveRoots);

$('alert-test').addEventListener('click', async () => {
  try {
    const res = await jsonApi('/api/alerts/test', { method: 'POST' });
    toast(res.ok ? 'Test message sent. Check Telegram.' : 'Not sent: ' + res.reason, res.ok ? undefined : 'error');
  } catch (exc) {
    toast('Not sent: ' + exc.message, 'error');
  }
});

// ---------------------------------------------------------------------------
// Refresh and clear cache
// ---------------------------------------------------------------------------

const menuBtn = $('menu-btn');

function refreshToast(res, force) {
  const n = fmtNum(res.requests) + ' request' + (res.requests === 1 ? '' : 's');
  if (res.joined) return 'A refresh was already running; its result is in: ' + n;
  return (force ? 'Rebuilt from the files: ' : 'Refreshed: ') + n;
}

let refreshing = false;
// One refresh at a time from this page; a second tap while one runs is ignored, and the server
// joins any that overlap from elsewhere.
async function runRefresh(force) {
  if (refreshing) return;
  refreshing = true;
  menuBtn.setAttribute('aria-busy', 'true');
  menuBtn.disabled = true;
  try {
    const res = await jsonApi('/api/refresh' + (force ? '?force=true' : ''), { method: 'POST' });
    if (!res.ok) {
      toast('Refresh failed: ' + res.reason, 'error');
      return;
    }
    toast(refreshToast(res, force));
    await refresh();
  } catch (exc) {
    console.error(exc);
    toast('Refresh failed: ' + exc.message, 'error');
  } finally {
    refreshing = false;
    menuBtn.disabled = false;
    menuBtn.removeAttribute('aria-busy');
  }
}

// The freshness readout is the anchor of a one-list floating menu (vendored row-menu). The vendored
// component builds the element itself, so its surface id is stamped here, at the call site.
const refreshMenu = createRowMenu().attach('refresh', menuBtn, [
  { label: 'Refresh', glyph: 'refresh-cw', dataset: { track: 'refresh' }, onTap: () => runRefresh(false) },
  { label: 'Force refresh', glyph: 'rotate-cw', dataset: { track: 'refresh-force' }, onTap: () => runRefresh(true) },
]);
refreshMenu.dataset.surface = 'chrome.refresh-menu';

$('cache-clear').addEventListener('click', async () => {
  try {
    const res = await jsonApi('/api/cache/clear', { method: 'POST' });
    if (!res.ok) throw new Error('refused');
    toast('Cache cleared. Rebuilding from the files.');
    await refresh();
  } catch (exc) {
    console.error(exc);
    toast('Could not clear the cache: ' + exc.message, 'error');
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

// Feature blocks are read before anything that one of them owns starts (the usage log, the billing poll).
// The server decides which blocks are off; a failed read leaves every block on.
const features = initFeatures({
  jsonApi,
  showToast: toast,
  // The data and the billing card follow the switches, so a toggle re-reads them.
  afterToggle: () => { _billingFetched = 0; refresh(); },
});
features.onChange(syncUsageLog);
await features.load();
initTailnet({ jsonApi, showToast: toast }).load();

fetch('/api/version').then((r) => r.json()).then((v) => {
  $('buildReadout').textContent = buildReadoutText(v.git_sha || 'unknown', v.built_at || '');
}).catch(() => {});

const filters = initFilters({ $, state, periodNames: PERIOD_NAMES, setPeriod, renderSessions });
initNavTabs({
  storageKey: 'copilot-usage.tab',
  // dynamic-id-exempt: the nav only ever passes one of the four constant data-tab names
  onChange: (tab) => { paintHeader(tab); filters.showFor(tab); usage.trackView(tab); },
});
initSections(document.querySelector('main.app'));
const showSessionName = initSessionName({ jsonApi, showToast: toast, reopen: openSession, refresh });
initTimeline({ state, jsonApi, setPeriod, renderCharts, showToast: toast });

const requests = initRequests({
  jsonApi, fmtCredits, fmtTok, fmtNum, fmtDay, fmtTime, openSession,
  getPeriod: () => state.period,
});

initInsights({
  jsonApi, fmtTok, fmtNum, fmtDay, fmtTime, openSession,
  openRequests: requests.open,
  getPeriod: () => state.period,
});

initModelSettings({
  $, jsonApi, esc, fmtNum, saveSetting, showToast: toast, colors,
  getSettings: () => settings,
});

// Colour overrides live in the config, so the charts repaint once it arrives.
jsonApi('/api/config').then((cfg) => {
  paintSettings(cfg);
  if (state.summary) render();
}).catch(() => {});

refresh();
setInterval(refresh, POLL_MS);
