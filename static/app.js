/* github-copilot-usage dashboard.
 *
 * Polls /api/summary every 30 s (plus /api/sessions and the optional
 * /api/billing). Charts are Chart.js (vendored — no CDN so it works behind
 * corporate proxies).
 *
 * Categorical palettes are CVD-validated (dataviz six-checks): fixed
 * assignment order, colors follow the model (never its rank), 8th+ series
 * folds into a gray "Other". Stacked segments carry a 2px surface gap as the
 * secondary encoding.
 */

/* global Chart */

const POLL_MS = 30_000;
const SESSIONS_PREVIEW = 12;

const PALETTE = {
  light: ['#0969da', '#bc4c00', '#8250df', '#1a7f37', '#bf3989', '#0685ab', '#bf8700'],
  dark:  ['#4493f8', '#dd6b20', '#a371f7', '#2ea043', '#db61a2', '#0f96aa', '#b3831a'],
};
const OTHER_COLOR = { light: '#656d76', dark: '#8b949e' };

const state = {
  period: 'today',
  summary: null,
  sessions: [],
  sessionsSort: 'credits',
  sessionsExpanded: false,
  modelSlots: new Map(),   // model name -> palette slot (stable for the page's lifetime)
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
// Formatting
// ---------------------------------------------------------------------------

function fmtNum(n) {
  if (n === undefined || n === null) return '—';
  return Number(n).toLocaleString();
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

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---------------------------------------------------------------------------
// Model colors — stable slot per model, fixed palette order
// ---------------------------------------------------------------------------

function modelColor(model) {
  if (!state.modelSlots.has(model)) {
    state.modelSlots.set(model, state.modelSlots.size);
  }
  const slot = state.modelSlots.get(model);
  const theme = currentTheme();
  if (slot >= PALETTE[theme].length) return OTHER_COLOR[theme];
  return PALETTE[theme][slot];
}

function seedModelSlots(byModel) {
  // Assign slots by total credits (descending) on first load so the most
  // expensive models take the leading palette hues; later models append.
  for (const row of byModel) modelColor(row.model);
}

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
  fetchBilling().catch(() => {});
}

let _billingFetched = 0;
async function fetchBilling() {
  if (Date.now() - _billingFetched < 60_000) return;
  _billingFetched = Date.now();
  const body = await jsonApi('/api/billing');
  renderBilling(body);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function render() {
  const s = state.summary;
  if (!s) return;
  seedModelSlots(s.by_model || []);
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
  $('t-avg').textContent = t.billed_requests
    ? fmtCredits(t.avg_credits_per_request) + ' cr/req' : '';
  $('t-in').textContent = fmtTok(t.prompt_tokens);
  $('t-out').textContent = fmtTok(t.completion_tokens);
  $('t-in-sub').textContent = t.requests
    ? '~' + fmtTok(t.prompt_tokens / Math.max(t.requests, 1)) + ' per request' : '';
  $('t-out-sub').textContent = t.errors
    ? t.errors + ' failed req (' + fmtCredits(t.error_credits) + ' cr)' : '';

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
  $('budget-note').textContent = 'Local data from this machine only — set your allowance in Settings.';
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

  // Model set across the window, in stable slot order.
  const models = [...new Set(ts.flatMap((b) => Object.keys(b.models || {})))];
  models.forEach(modelColor);
  models.sort((a, b) => state.modelSlots.get(a) - state.modelSlots.get(b));

  const creditDatasets = models
    .map((m) => ({
      label: m,
      data: ts.map((b) => (b.models?.[m]?.credits) || 0),
      backgroundColor: modelColor(m),
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

  const accent = modelColor(models[0] || 'default');
  const theme = currentTheme();
  const inColor = PALETTE[theme][0];
  const outColor = PALETTE[theme][1];
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
  void accent;
}

// ---- prompt mix ----

function renderMix(mix, card, container) {
  if (!mix.length) { card.hidden = true; return; }
  card.hidden = false;
  container.innerHTML = mix.map((row) => `
    <div class="mix-row">
      <div class="mix-top">
        <span>${esc(row.label)}</span>
        <span class="pct">${row.pct}% · ~${fmtTok(row.est_tokens)} tok</span>
      </div>
      <div class="mix-track"><div class="mix-fill" style="width:${Math.min(100, row.pct)}%"></div></div>
    </div>`).join('');
}

// ---- tables ----

function renderModelTable(rows) {
  const tbody = $('model-table').querySelector('tbody');
  $('model-empty').hidden = rows.length > 0;
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td class="td-trunc" title="${esc(r.model)}"><span class="model-dot" style="background:${modelColor(r.model)}"></span>${esc(r.model)}</td>
      <td class="num">${fmtNum(r.requests)}</td>
      <td class="num">${fmtCredits(r.credits)}</td>
      <td class="num">${r.credits_share_pct}%</td>
      <td class="num">${fmtTok(r.prompt_tokens)} / ${fmtTok(r.completion_tokens)}</td>
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
      <td class="num">${fmtTok(r.prompt_tokens)} / ${fmtTok(r.completion_tokens)}</td>
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
    <li data-sid="${esc(s.session_id)}" tabindex="0" role="button" aria-label="Open session detail">
      <span class="sess-title">${esc(s.title)}</span>
      <span class="sess-credits">${fmtCredits(s.credits)} cr</span>
      <span class="sess-meta">
        ${esc(s.project)} · ${esc(s.ide)} · ${fmtDay(s.last_ts)} ${fmtTime(s.first_ts)}–${fmtTime(s.last_ts)}
        · ${fmtNum(s.requests)} req · ${fmtTok(s.prompt_tokens)}↑ ${fmtTok(s.completion_tokens)}↓
        · ${esc((s.models || []).join(', '))}
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
    + fmtCredits(detail.credits) + ' credits (≈ ' + fmtUsd(detail.usd) + ')';

  const mix = detail.prompt_mix || [];
  $('sd-mix').innerHTML = mix.length ? mix.map((row) => `
    <div class="mix-row">
      <div class="mix-top"><span>${esc(row.label)}</span><span class="pct">${row.pct}% · ~${fmtTok(row.est_tokens)} tok</span></div>
      <div class="mix-track"><div class="mix-fill" style="width:${Math.min(100, row.pct)}%"></div></div>
    </div>`).join('') : '';

  const tbody = $('sd-table').querySelector('tbody');
  tbody.innerHTML = (detail.requests_detail || []).map((r) => `
    <tr${r.error ? ' style="opacity:0.6"' : ''}>
      <td class="num">${fmtTime(r.ts)}</td>
      <td class="td-trunc" title="${esc(r.message)}">${esc(r.message) || '<span class="muted">—</span>'}${r.error ? ' ⚠️' : ''}</td>
      <td>${esc(r.mode)}</td>
      <td class="td-trunc" title="${esc(r.model)}">${esc(r.model)}</td>
      <td class="num">${fmtTok(r.prompt_tokens)}</td>
      <td class="num">${fmtTok(r.completion_tokens)}</td>
      <td class="num">${r.credits === null ? '—' : fmtCredits(r.credits)}</td>
      <td class="num">${r.elapsed_ms === null ? '—' : (r.elapsed_ms / 1000).toFixed(1) + 's'}</td>
    </tr>`).join('');

  $('session-dialog').showModal();
}

$('sd-close').addEventListener('click', () => $('session-dialog').close());

// ---- billing ----

function renderBilling(body) {
  const rows = (body && body.daily) || [];
  const tbody = $('billing-table').querySelector('tbody');
  $('billing-asof').textContent = body && body.as_of ? 'as of ' + fmtTime(body.as_of) : '';
  if (!body || !body.available || !rows.length) {
    $('billing-wrap').hidden = true;
    $('billing-note').textContent = body && body.available === false
      ? (body.reason || 'Not available.')
      : 'No account-level billing data in this window.';
    return;
  }
  $('billing-wrap').hidden = false;
  const total = rows.reduce((acc, r) => acc + (Number(r.credits) || 0), 0);
  $('billing-note').textContent =
    'Account-wide official spend (all devices), last 14 days: '
    + fmtCredits(total) + ' credits ≈ ' + fmtUsd(total * 0.01) + '.';
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

$('settings-btn').addEventListener('click', async () => {
  try {
    const cfg = await jsonApi('/api/config');
    $('set-allowance').value = cfg.monthly_credits;
    $('set-reset').value = cfg.cycle_reset_day;
    $('set-roots').value = (cfg.extra_roots || []).join('\n');
    $('set-cli').setAttribute('aria-checked', String(!!cfg.include_copilot_cli));
    $('set-pat-note').textContent = cfg.billing_pat_configured
      ? 'GitHub billing PAT: configured ✓'
      : 'GitHub billing PAT: not set — add GITHUB_COPILOT_BILLING_PAT to .env for the official billing card.';
  } catch { /* open anyway with stale values */ }
  $('settings-dialog').showModal();
});

$('set-cli').addEventListener('click', () => {
  const el = $('set-cli');
  el.setAttribute('aria-checked', String(el.getAttribute('aria-checked') !== 'true'));
});

$('settings-close').addEventListener('click', () => $('settings-dialog').close());

$('settings-save').addEventListener('click', async () => {
  const updates = {
    monthly_credits: Number($('set-allowance').value) || 0,
    cycle_reset_day: Number($('set-reset').value) || 1,
    extra_roots: $('set-roots').value.split('\n').map((s) => s.trim()).filter(Boolean),
    include_copilot_cli: $('set-cli').getAttribute('aria-checked') === 'true',
  };
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    $('settings-dialog').close();
    refresh();
  } catch (exc) {
    console.error(exc);
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

fetch('/health').then((r) => r.json()).then((h) => {
  $('version-tag').textContent = 'github-copilot-usage v' + h.version;
}).catch(() => {});

refresh();
setInterval(refresh, POLL_MS);
