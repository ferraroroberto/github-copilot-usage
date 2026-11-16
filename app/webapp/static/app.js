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

/* global Chart */

import { buildReadoutText } from './_vendored/page-foot/page-foot.js';
import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { setSwitch } from './_vendored/switch/switch.js';
import { initRequests } from './requests.js';
import { flagChips, initInsights, refreshInsights, renderDiagnostics } from './insights.js';
import { createModelColors, initModelSettings, seriesColor, summarizeModelSettings } from './models.js';

// Local-only usage log (views, taps, device class); same-origin, no content recorded.
const usage = initUsageLog();
// Single-view page: the base view the dialogs return to when they close.
usage.trackView('dashboard');

const POLL_MS = 30_000;
// Billing changes server-side at most every 5 min (src/billing.py caches today),
// so the 30 s refresh need not re-ask each time; 60 s is a stated guess, not a
// measurement, and is deliberately shorter than that cache.
const BILLING_POLL_MS = 60_000;
const SESSIONS_PREVIEW = 12;

const state = {
  period: 'today',
  summary: null,
  sessions: [],
  sessionsSort: 'credits',
  sessionsExpanded: false,
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
  if (state.summary) renderCharts(state.summary);   // re-resolve palette + axis colors
});

// ---------------------------------------------------------------------------
// Text size — the zoom-lock escape (the pre-paint stamp lives in index.html)
// ---------------------------------------------------------------------------

const TEXT_SIZES = ['small', 'default', 'large'];

function paintTextSize() {
  const size = document.documentElement.dataset.textsize || 'default';
  document.querySelectorAll('#set-textsize [data-textsize]').forEach((btn) => {
    const on = btn.dataset.textsize === size;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', String(on));
  });
}

$('set-textsize').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-textsize]');
  if (!btn || !TEXT_SIZES.includes(btn.dataset.textsize)) return;
  document.documentElement.dataset.textsize = btn.dataset.textsize;
  try { localStorage.setItem('copilot-usage.textsize', btn.dataset.textsize); } catch { /* applies for this view only */ }
  paintTextSize();
  showToast('Text size: ' + btn.textContent.trim().toLowerCase());
});
paintTextSize();

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtNum(n) {
  if (n === undefined || n === null) return '—';
  return Number(n).toLocaleString();
}

// One-line token breakdown for a row's tooltip; cache buckets stay separate from input volume.
function tokBreakdown(r) {
  return 'uncached in ' + fmtNum(r.uncached_input_tokens)
    + ' · cache read ' + fmtNum(r.cache_read_tokens)
    + ' · cache write ' + fmtNum(r.cache_write_tokens)
    + ' · out ' + fmtNum(r.completion_tokens)
    + ' · total in ' + fmtNum(r.total_input_tokens)
    + ' · effective ' + fmtNum(r.effective_input_tokens) + ' in / ' + fmtNum(r.effective_output_tokens) + ' out'
    + (r.tokens_per_credit == null ? '' : ' · ' + fmtNum(r.tokens_per_credit) + ' tok/credit');
}

function cacheLine(r) {
  const bits = [];
  if (r.cache_read_tokens) bits.push(fmtTok(r.cache_read_tokens) + ' cache read');
  if (r.cache_write_tokens) bits.push(fmtTok(r.cache_write_tokens) + ' cache write');
  return bits.join(' · ');
}

function fmtTok(n) {
  n = Number(n) || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + 'k';
  return String(n);
}

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

function esc(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
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
  monthly_credits: 0, cycle_reset_day: 1, extra_roots: [], include_copilot_cli: true,
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

async function refresh() {
  try {
    const [summary, sessions] = await Promise.all([
      jsonApi('/api/summary?period=' + state.period),
      jsonApi('/api/sessions?period=' + state.period),
    ]);
    state.summary = summary;
    state.sessions = sessions.sessions || [];
    render();
    $('freshness').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (exc) {
    $('freshness').textContent = 'error fetching data';
    console.error(exc);
  }
  refreshInsights();
  fetchBilling().catch(() => {});
}

let _billingFetched = 0;
async function fetchBilling() {
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
  renderBudget(s.budget);
  renderCharts(s);
  renderMix(s.prompt_mix || [], $('mix-card'), $('mix-bars'));
  renderModelTable(s.by_model || []);
  renderProjectTable(s.by_project || [], s.by_mode || []);
  renderSessions();
  renderSources(s.sources || []);
  $('export-link').href = '/api/export.csv?period=' + state.period;
}

function renderTiles(s) {
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
  $('t-in-sub').textContent = cacheLine(t) || (t.requests
    ? '~' + fmtTok(t.prompt_tokens / Math.max(t.requests, 1)) + ' per request' : '');
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

function renderBudget(b) {
  if (!b) { $('budget-card').hidden = true; return; }
  $('budget-card').hidden = false;
  $('budget-range').textContent = fmtDay(b.cycle_start) + ' – ' + fmtDay(b.cycle_end)
    + ' · day ' + b.days_elapsed + '/' + b.days_total;

  const pct = Math.min(100, b.used_pct || 0);
  const fill = $('budget-fill');
  fill.style.width = pct + '%';
  fill.className = 'budget-fill'
    + ((b.used_pct || 0) >= 100 ? ' over' : (b.projected_pct || 0) > 100 ? ' warn' : '');

  const marker = $('budget-today-marker');
  marker.style.left = Math.min(100, (b.days_elapsed / b.days_total) * 100) + '%';

  $('budget-used').textContent =
    fmtCredits(b.used_credits) + ' / ' + fmtNum(b.allowance_credits)
    + ' credits (' + (b.used_pct ?? 0) + '%)';
  $('budget-projection').textContent =
    'projected ' + fmtCredits(b.projected_credits) + ' (' + (b.projected_pct ?? 0) + '%) by cycle end';
}

// ---- charts ----

let _chartCredits = null;
let _chartTokens = null;

function axisOptions(isTok) {
  const grid = cssVar('--border-muted');
  const tick = cssVar('--fg-muted');
  return {
    x: {
      stacked: true,
      grid: { display: false },
      ticks: { color: tick, font: { size: 11 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
    },
    y: {
      stacked: true,
      beginAtZero: true,
      grid: { color: grid },
      ticks: { color: tick, font: { size: 11 }, callback: isTok ? (v) => fmtTok(v) : undefined },
    },
  };
}

function legendOptions() {
  return {
    position: 'bottom',
    labels: { color: cssVar('--fg'), boxWidth: 10, boxHeight: 10, padding: 12, font: { size: 11 }, usePointStyle: true },
  };
}

function renderCharts(s) {
  const ts = s.time_series || [];
  const card = $('charts-card');
  if (!ts.length) { card.hidden = true; return; }
  card.hidden = false;

  const labels = ts.map((b) => b.label);
  const surface = cssVar('--card');

  // Model set across the window, family by family in stable rank order.
  const models = colors.stackOrder([...new Set(ts.flatMap((b) => Object.keys(b.models || {})))]);

  const creditDatasets = models
    .map((m) => ({
      label: m,
      data: ts.map((b) => (b.models?.[m]?.credits) || 0),
      backgroundColor: colors.colorOf(m),
      borderColor: surface,          // 2px surface gap between stacked segments
      borderWidth: 1,
      borderRadius: 3,
      maxBarThickness: 34,
    }))
    .filter((d) => d.data.some((v) => v > 0));

  if (_chartCredits) _chartCredits.destroy();
  _chartCredits = new Chart($('chart-credits'), {
    type: 'bar',
    data: { labels, datasets: creditDatasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: axisOptions(false),
      plugins: {
        legend: legendOptions(),
        tooltip: {
          callbacks: {
            label: (ctx) => ' ' + ctx.dataset.label + ': ' + fmtCredits(ctx.parsed.y) + ' cr',
          },
        },
      },
    },
  });

  renderTokenChart(ts, labels);
}

function renderTokenChart(ts, labels) {
  const theme = currentTheme();
  const inColor = seriesColor(theme, 0);
  const outColor = seriesColor(theme, 1);
  const cacheColor = seriesColor(theme, 2);
  const sumField = (b, f) => Object.values(b.models || {}).reduce((acc, m) => acc + (m[f] || 0), 0);

  if (_chartTokens) _chartTokens.destroy();
  _chartTokens = new Chart($('chart-tokens'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Tokens in',
          data: ts.map((b) => sumField(b, 'prompt_tokens')),
          borderColor: inColor,
          backgroundColor: inColor + '2e',
          fill: true, tension: 0.3, borderWidth: 2,
          pointRadius: labels.length > 10 ? 0 : 3, pointHoverRadius: 4,
        },
        {
          label: 'Tokens out',
          data: ts.map((b) => sumField(b, 'completion_tokens')),
          borderColor: outColor,
          backgroundColor: outColor + '2e',
          fill: true, tension: 0.3, borderWidth: 2,
          pointRadius: labels.length > 10 ? 0 : 3, pointHoverRadius: 4,
        },
        {
          label: 'Cache read (not in tokens in)',
          data: ts.map((b) => sumField(b, 'cache_read_tokens')),
          borderColor: cacheColor,
          borderDash: [5, 4],
          fill: false, tension: 0.3, borderWidth: 2,
          pointRadius: labels.length > 10 ? 0 : 3, pointHoverRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: (() => { const o = axisOptions(true); o.x.stacked = false; o.y.stacked = false; return o; })(),
      plugins: {
        legend: legendOptions(),
        tooltip: {
          callbacks: { label: (ctx) => ' ' + ctx.dataset.label + ': ' + fmtTok(ctx.parsed.y) },
        },
      },
    },
  });
}

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

function renderSessions() {
  const rows = sortedSessions();
  $('sessions-count').textContent = rows.length;
  $('sessions-empty').hidden = rows.length > 0;
  const shown = state.sessionsExpanded ? rows : rows.slice(0, SESSIONS_PREVIEW);
  $('sessions-more').hidden = rows.length <= SESSIONS_PREVIEW;
  $('sessions-more').textContent = state.sessionsExpanded
    ? 'Show fewer' : 'Show all ' + rows.length;

  $('session-list').innerHTML = shown.map((s) => `
    <li data-track="session-open" data-sid="${esc(s.session_id)}" tabindex="0" role="button" aria-label="Open session detail">
      <span class="sess-title">${esc(s.title)}</span>
      <span class="sess-credits">${fmtCredits(s.credits)} cr</span>
      <span class="sess-meta">
        ${esc(s.project)} · ${esc(s.ide)} · ${fmtDay(s.last_ts)} ${fmtTime(s.first_ts)}–${fmtTime(s.last_ts)}
        · ${fmtNum(s.requests)} req · ${fmtTok(s.prompt_tokens)}<svg class="inline-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="in"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg> ${fmtTok(s.completion_tokens)}<svg class="inline-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="out"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>
        · ${esc((s.models || []).join(', '))}${s.estimated_requests ? ' · estimated' : ''}
        ${flagChips(s.flags)}
      </span>
    </li>`).join('');
}

$('sessions-sort').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-sort]');
  if (!btn) return;
  state.sessionsSort = btn.dataset.sort;
  $('sessions-sort').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
  renderSessions();
});

$('sessions-more').addEventListener('click', () => {
  state.sessionsExpanded = !state.sessionsExpanded;
  renderSessions();
});

$('session-list').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-sid]');
  if (li) openSession(li.dataset.sid);
});
$('session-list').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const li = e.target.closest('li[data-sid]');
  if (li) { e.preventDefault(); openSession(li.dataset.sid); }
});

async function openSession(sid) {
  let detail;
  try {
    detail = await jsonApi('/api/sessions/' + encodeURIComponent(sid));
  } catch { return; }
  if (detail.error) return;

  $('sd-title').textContent = detail.title || sid;
  $('sd-meta').textContent =
    detail.project + ' · ' + detail.ide
    + ' · ' + fmtDay(detail.first_ts) + ' ' + fmtTime(detail.first_ts) + ' – ' + fmtTime(detail.last_ts)
    + ' · ' + fmtNum(detail.requests) + ' requests · '
    + fmtCredits(detail.credits) + ' credits (≈ ' + fmtUsd(detail.usd) + ')'
    + (cacheLine(detail) ? ' · ' + cacheLine(detail) : '')
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
  tbody.innerHTML = (detail.requests_detail || []).map((r) => {
    const est = r.usage_estimated ? '~' : '';
    return `
    <tr${r.error ? ' style="opacity:0.6"' : ''}${r.usage_estimated ? ' title="Estimated: allocated from session totals (' + esc(r.usage_source) + ')"' : ''}>
      <td class="num">${fmtTime(r.ts)}</td>
      <td class="td-trunc" title="${esc(r.message)}">${esc(r.message) || '<span class="muted">—</span>'}${r.error ? ' <svg class="inline-glyph glyph-warn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="errored"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>' : ''}</td>
      <td>${esc(r.mode)}</td>
      <td class="td-trunc" title="${esc(r.model)}">${esc(r.model)}</td>
      <td class="num" title="${esc(tokBreakdown(r))}">${est}${fmtTok(r.prompt_tokens)}</td>
      <td class="num">${est}${fmtTok(r.completion_tokens)}</td>
      <td class="num">${r.credits === null ? '—' : est + fmtCredits(r.credits)}</td>
      <td class="num">${r.elapsed_ms === null ? '—' : (r.elapsed_ms / 1000).toFixed(1) + 's'}</td>
    </tr>`;
  }).join('');

  $('session-dialog').showModal();
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
    </li>`).join('') || '<li><span class="muted">No Copilot data locations found.</span></li>';
}

// ---------------------------------------------------------------------------
// Period switching
// ---------------------------------------------------------------------------

$('period-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-period]');
  if (!btn || btn.dataset.period === state.period) return;
  state.period = btn.dataset.period;
  state.sessionsExpanded = false;
  $('period-seg').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
  refresh();
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

let toastTimer = null;
// The toast is a manual popover so it paints above an open modal dialog; where
// popovers are missing the .show class alone still displays it.
function showToast(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  if (isError) el.dataset.tone = 'danger'; else delete el.dataset.tone;
  el.setAttribute('aria-live', isError ? 'assertive' : 'polite');
  el.classList.add('show');
  try { if (el.showPopover && !el.matches(':popover-open')) el.showPopover(); } catch { /* shown by the class */ }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
    try { if (el.hidePopover && el.matches(':popover-open')) el.hidePopover(); } catch { /* already hidden */ }
  }, 2600);
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
    showToast(message);
    refresh();
    $('set-models-value').textContent = summarizeModelSettings(settings);
    return saved;
  } catch (exc) {
    console.error(exc);
    showToast('Could not save that setting. Try again.', true);
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
  setSwitch($('set-cli'), !!cfg.include_copilot_cli);
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

$('set-cli').addEventListener('click', async () => {
  const el = $('set-cli');
  const next = el.getAttribute('aria-checked') !== 'true';
  setSwitch(el, next);
  const saved = await saveSetting('include_copilot_cli', next,
    next ? 'Copilot CLI sessions included' : 'Copilot CLI sessions left out');
  if (!saved) setSwitch(el, !next);
});

$('roots-open').addEventListener('click', () => {
  $('set-roots').value = (settings.extra_roots || []).join('\n');
  $('roots-dialog').showModal();
});

$('roots-close').addEventListener('click', () => $('roots-dialog').close());

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
    showToast(res.ok ? 'Test message sent. Check Telegram.' : 'Not sent: ' + res.reason, !res.ok);
  } catch (exc) {
    showToast('Not sent: ' + exc.message, true);
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

fetch('/api/version').then((r) => r.json()).then((v) => {
  $('buildReadout').textContent = buildReadoutText(v.git_sha || 'unknown', v.built_at || '');
}).catch(() => {});

const requests = initRequests({
  jsonApi, esc, fmtCredits, fmtTok, fmtNum, fmtDay, fmtTime, openSession,
  getPeriod: () => state.period,
});

initInsights({
  jsonApi, esc, fmtTok, fmtNum, fmtDay, fmtTime, openSession,
  openRequests: requests.open,
  getPeriod: () => state.period,
});

initModelSettings({
  $, jsonApi, esc, fmtNum, saveSetting, showToast, colors,
  getSettings: () => settings,
});

// Colour overrides live in the config, so the charts repaint once it arrives.
jsonApi('/api/config').then((cfg) => {
  paintSettings(cfg);
  if (state.summary) render();
}).catch(() => {});

refresh();
setInterval(refresh, POLL_MS);
