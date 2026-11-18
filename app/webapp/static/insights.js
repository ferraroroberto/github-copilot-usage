/* Insights card, session diagnostics and the context drill-down dialog.
 *
 * The server (GET /api/insights, GET /api/context) does every calculation; this module only
 * draws the answers and sends a tap to the view the evidence names: a session opens its
 * detail, a label opens the drill-down, a project or model opens the request explorer
 * already filtered, anything else (a period, the billing cycle) opens it for that period.
 */

import { actionRowHtml, esc, splitMeta } from './rows.js';

const SEVERITY_LABEL = { critical: 'Critical', warning: 'Warning', info: 'Info' };
// What an insight is about, as the row's title; its sentence is the status line.
const TYPE_LABEL = {
  top_contributor: 'Biggest contributor',
  period_change: 'Spend change',
  budget_risk: 'Budget',
  prompt_composition: 'Prompt tokens',
};
const FLAG_LABEL = {
  large_prompt: 'Large prompt',
  growing_prompt: 'Growing prompt',
  high_non_user_share: 'High overhead',
};
const PERIOD_LABEL = {
  today: 'Today', week: 'Last 7 days', month: 'Last 30 days', cycle: 'This billing cycle', all: 'All time',
};
const RANGE = /^range:(\d{4})-(\d{2})-(\d{2})\.\.(\d{4})-(\d{2})-(\d{2})$/;

// A period's name for a heading: a fixed period, or the days of a focused range.
export function periodLabel(period) {
  if (PERIOD_LABEL[period]) return PERIOD_LABEL[period];
  const m = RANGE.exec(period || '');
  if (!m) return period || '';
  const day = (y, mo, d) => new Date(Number(y), Number(mo) - 1, Number(d))
    .toLocaleDateString([], { month: 'short', day: 'numeric' });
  const first = day(m[1], m[2], m[3]);
  const last = day(m[4], m[5], m[6]);
  return first === last ? first : first + ' – ' + last;
}

// Placeholders explain an empty or sparse period; there is no view behind them.
const STATIC_TYPES = new Set(['no_data', 'insufficient_evidence']);

const $ = (id) => document.getElementById(id);

let deps = null;          // formatters and callbacks handed over by the dashboard
let shown = [];           // the insights on screen, in order, for the click handler
let insightSeq = 0;       // drops a response that a newer period has overtaken
let ctxSeq = 0;
let ctx = { period: 'week', label: '' };

// ---- shared pieces ----

export function flagChips(flags) {
  return (flags || []).map((f) =>
    `<span class="badge flag" title="${esc(f.text)}">${esc(FLAG_LABEL[f.type] || f.type)}</span>`).join(' ');
}

// The session dialog's diagnostics block: the facts, then each flag with its heuristic wording.
export function renderDiagnostics(el, diag) {
  const { fmtTok } = deps;
  if (!diag || !diag.max_prompt_tokens) { el.hidden = true; el.innerHTML = ''; return; }
  const facts = ['largest prompt ' + fmtTok(diag.max_prompt_tokens)];
  if (diag.growth_ratio) facts.push('last prompt ' + diag.growth_ratio + '× the first');
  if (diag.non_user_pct !== null && diag.non_user_pct !== undefined) {
    facts.push('~' + diag.non_user_pct + '% outside your own context (est.)');
  }
  const flags = (diag.flags || []).map((f) =>
    `<li><span class="badge flag">${esc(FLAG_LABEL[f.type] || f.type)}</span> ${esc(f.text)}</li>`).join('');
  el.innerHTML = `<p class="dialog-meta">${esc(facts.join(' · '))}</p>`
    + (flags ? `<ul class="flag-list">${flags}</ul>` : '');
  el.hidden = false;
}

// ---- insights card ----

// Only an exception wears a chip: an insight that is merely informative reads as plain text.
function severityChip(severity) {
  if (severity === 'info') return '';
  return ` <span class="sev sev-${esc(severity)}">${esc(SEVERITY_LABEL[severity] || severity)}</span>`;
}

function insightRow(insight, i) {
  if (STATIC_TYPES.has(insight.type)) {
    return `<li class="insight-static">${esc(insight.text)}</li>`;
  }
  return actionRowHtml({
    track: 'insight-open',
    data: { i },
    title: TYPE_LABEL[insight.type] || insight.type,
    meta: `<span>${esc(insight.text)}${severityChip(insight.severity)}</span>`,
  });
}

function renderInsights(body) {
  const { fmtNum } = deps;
  shown = body.insights || [];
  $('insights-card').hidden = false;
  $('insight-list').innerHTML = shown.map(insightRow).join('');
  $('insight-quiet').hidden = !(body.state === 'ok' && shown.length === 0);
  $('insight-quiet').textContent = 'Nothing stands out in this period.';
  const cov = body.coverage || {};
  $('insights-meta').textContent = cov.requests_total
    ? 'breakdown on ' + fmtNum(cov.requests_covered) + ' of ' + fmtNum(cov.requests_total) + ' requests'
    : '';
}

function insightsFailed() {
  shown = [];
  $('insights-card').hidden = false;
  $('insight-list').innerHTML = '';
  $('insights-meta').textContent = '';
  $('insight-quiet').hidden = false;
  $('insight-quiet').textContent = 'Insights could not be loaded from the dashboard server.';
}

export async function refreshInsights() {
  const seq = ++insightSeq;
  try {
    const body = await deps.jsonApi('/api/insights?period=' + encodeURIComponent(deps.getPeriod()));
    if (seq === insightSeq) renderInsights(body);
  } catch (exc) {
    if (seq !== insightSeq) return;
    console.error(exc);
    insightsFailed();
  }
}

function openInsight(insight) {
  const ev = insight.evidence || {};
  if (ev.session) { deps.openSession(ev.session); return; }
  if (ev.label) { openContext(ev.label, ev.period); return; }
  const filters = {};
  if (ev.period) filters.period = ev.period;
  if (ev.project) filters.project = ev.project;
  if (ev.model) filters.model = ev.model;
  deps.openRequests(filters);
}

// ---- context drill-down ----

function chipHtml(label, text, active) {
  return `<button data-track="context-label" class="chip${active ? ' active' : ''}" type="button" data-label="${esc(label)}" aria-pressed="${active}">${esc(text)}</button>`;
}

function renderChips(labels) {
  const known = labels.some((l) => l.label === ctx.label);
  const chips = [chipHtml('', 'All of the prompt', ctx.label === '')];
  if (ctx.label && !known) chips.push(chipHtml(ctx.label, ctx.label, true));
  for (const l of labels) chips.push(chipHtml(l.label, l.label + ' ' + l.pct + '%', l.label === ctx.label));
  $('cx-labels').innerHTML = chips.join('');
}

function contextRow(kind, key, title, share, sub, flags) {
  return actionRowHtml({
    track: 'context-open-row',
    data: { kind, key },
    title,
    blank: '(no prompt text)',
    meta: splitMeta(sub, share === null || share === undefined ? '—' : share + '%', flagChips(flags)),
    share: share || 0,
  });
}

function fillLists(body) {
  const { fmtTok, fmtNum, fmtDay, fmtTime } = deps;
  const tok = (n) => '~' + fmtTok(n) + ' tok';
  const reqs = (n) => fmtNum(n) + (n === 1 ? ' request' : ' requests');
  $('cx-projects').innerHTML = body.projects.map((p) =>
    contextRow('project', p.project_key, p.project, p.share_pct, tok(p.est_tokens) + ' · ' + reqs(p.requests))).join('');
  $('cx-models').innerHTML = body.models.map((m) =>
    contextRow('model', m.model, m.model, m.share_pct, tok(m.est_tokens) + ' · ' + reqs(m.requests))).join('');
  $('cx-sessions').innerHTML = body.sessions.map((s) =>
    contextRow('session', s.session_id, s.title, s.share_pct,
      tok(s.est_tokens) + ' · ' + reqs(s.requests) + ' · ' + s.project, s.flags)).join('');
  $('cx-requests').innerHTML = body.requests.map((r) =>
    contextRow('request', r.session_id, r.message, r.share_pct,
      tok(r.est_tokens) + ' of ' + fmtTok(r.prompt_tokens) + ' · ' + fmtDay(r.ts) + ' ' + fmtTime(r.ts)
      + ' · ' + r.project + ' · ' + r.model)).join('');
}

// Files, tools and attachments by how many requests carried them: names only, no content.
function renderContextItems(items, requestsTotal) {
  const { fmtNum } = deps;
  $('cx-items-empty').hidden = items.length > 0;
  $('cx-items-empty').textContent = items.length
    ? '' : 'No attached files, tools or attachments were logged for this period.';
  $('cx-items-list').innerHTML = items.map((it) => `
    <li class="mix-row">
      <div class="mix-top"><span class="cx-item-name" title="${esc(it.label)}">${esc(it.label)}</span><span class="pct">${fmtNum(it.requests)} ${it.requests === 1 ? 'request' : 'requests'}</span></div>
      <div class="mix-track"><div class="mix-fill" style="width:${requestsTotal ? Math.min(100, 100 * it.requests / requestsTotal) : 0}%"></div></div>
    </li>`).join('');
}

function coverageText(cov) {
  const { fmtNum } = deps;
  if (!cov.requests_total) return '';
  const base = 'Breakdown recorded for ' + fmtNum(cov.requests_covered) + ' of ' + fmtNum(cov.requests_total)
    + ' requests (' + (cov.tokens_pct === null ? 0 : cov.tokens_pct) + '% of prompt tokens).';
  return cov.requests_covered < cov.requests_total
    ? base + ' The rest has no logged breakdown and is not counted below.'
    : base;
}

function renderContext(body) {
  $('cx-title').textContent = body.label || 'Prompt context';
  $('cx-meta').textContent = periodLabel(body.period);
  renderChips(body.labels || []);
  $('cx-coverage').textContent = coverageText(body.coverage);
  renderContextItems(body.context_items || [], (body.coverage || {}).requests_total || 0);
  $('cx-note').textContent = body.note || '';
  const ok = body.state === 'ok';
  $('cx-body').hidden = !ok;
  $('cx-empty').hidden = ok;
  $('cx-empty-text').textContent = body.reason || '';
  if (ok) fillLists(body);
}

async function loadContext() {
  const seq = ++ctxSeq;
  $('cx-title').textContent = ctx.label || 'Prompt context';
  $('cx-meta').textContent = periodLabel(ctx.period) + ' · loading…';
  try {
    const body = await deps.jsonApi('/api/context?period=' + encodeURIComponent(ctx.period)
      + '&label=' + encodeURIComponent(ctx.label));
    if (seq === ctxSeq) renderContext(body);
  } catch (exc) {
    if (seq !== ctxSeq) return;
    console.error(exc);
    $('cx-body').hidden = true;
    $('cx-empty').hidden = false;
    $('cx-empty-text').textContent = 'Could not load the drill-down from the dashboard server.';
  }
}

function openContext(label, period) {
  ctx = { period: period || deps.getPeriod(), label: label || '' };
  if (!$('context-dialog').open) $('context-dialog').showModal();
  loadContext();
}

function openContextRow(row) {
  const { kind, key } = row.dataset;
  const period = ctx.period;
  if (kind === 'project') deps.openRequests({ period, project: key });
  else if (kind === 'model') deps.openRequests({ period, model: key });
  else deps.openSession(key);
}

// ---- wiring ----

// The rows are real buttons, so Enter and Space already arrive as a click.
function onRowTap(list, selector, handler) {
  list.addEventListener('click', (e) => {
    const row = e.target.closest(selector);
    if (row) handler(row);
  });
}

export function initInsights(handedOver) {
  deps = handedOver;
  onRowTap($('insight-list'), 'button[data-i]', (row) => openInsight(shown[Number(row.dataset.i)]));
  $('mix-bars').addEventListener('click', (e) => {
    const row = e.target.closest('button[data-label]');
    if (row) openContext(row.dataset.label, deps.getPeriod());
  });
  $('context-open').addEventListener('click', () => openContext('', deps.getPeriod()));
  $('cx-close').addEventListener('click', () => $('context-dialog').close());
  $('cx-labels').addEventListener('click', (e) => {
    const chip = e.target.closest('button[data-label]');
    if (chip && chip.dataset.label !== ctx.label) { ctx.label = chip.dataset.label; loadContext(); }
  });
  for (const id of ['cx-projects', 'cx-models', 'cx-sessions', 'cx-requests']) {
    onRowTap($(id), 'button[data-kind]', openContextRow);
  }
}
