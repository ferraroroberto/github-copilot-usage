/* Request explorer: the searchable, filterable, sortable list of every request in a period.
 *
 * The view lives in a dialog over the dashboard. Its whole state is in the URL
 * (?view=requests&period=week&model=...), so a filtered view can be bookmarked and a
 * reload restores it. The server (GET /api/requests) does the filtering, sorting,
 * paging and the facet counts; this module only draws them.
 */

import { actionRowHtml, esc, splitMeta } from './rows.js';

const DEFAULTS = {
  period: 'week', project: '', model: '', mode: '', ide: '', result: '',
  min: '', max: '', q: '', sort: 'ts', order: 'desc', page: 1,
};
const FACETS = ['project', 'model', 'mode', 'ide', 'result'];
// The fields inside the Filters group; the search box and the sort sit outside it.
const BOX_KEYS = [...FACETS, 'min', 'max'];
const RESULT_LABELS = { ok: 'Succeeded', error: 'Errored' };
// [descending, ascending] wording per sort column, for the order toggle.
const ORDER_LABELS = {
  ts: ['Newest first', 'Oldest first'],
  credits: ['Most credits first', 'Fewest credits first'],
  tokens: ['Most tokens first', 'Fewest tokens first'],
  elapsed: ['Slowest first', 'Fastest first'],
};
// URL/filter key -> API parameter, for the keys the API names differently or leaves optional.
const API_PARAMS = {
  project: 'project', model: 'model', mode: 'mode', ide: 'ide', result: 'result',
  min: 'min_credits', max: 'max_credits', q: 'q',
};
const WIDE_QUERY = '(min-width: 560px)';
const SEARCH_DEBOUNCE_MS = 300;

const $ = (id) => document.getElementById(id);

let deps = null;          // formatters and callbacks handed over by the dashboard
let filters = { ...DEFAULTS };
let loadSeq = 0;          // drops a response that a newer query has overtaken
let searchTimer = null;

// ---- URL state ----

function readUrl() {
  const p = new URLSearchParams(window.location.search);
  if (p.get('view') !== 'requests') return null;
  const next = { ...DEFAULTS };
  for (const key of Object.keys(DEFAULTS)) {
    if (p.has(key)) next[key] = key === 'page' ? Math.max(parseInt(p.get(key), 10) || 1, 1) : p.get(key);
  }
  return next;
}

function writeUrl() {
  // A response that lands after the dialog closed must not put the view back in the URL.
  if (!$('requests-dialog').open) return;
  const p = new URLSearchParams({ view: 'requests', period: filters.period });
  for (const key of Object.keys(DEFAULTS)) {
    if (key !== 'period' && String(filters[key]) !== String(DEFAULTS[key])) p.set(key, filters[key]);
  }
  history.replaceState(null, '', window.location.pathname + '?' + p.toString());
}

function clearUrl() {
  history.replaceState(null, '', window.location.pathname);
}

function apiUrl() {
  const p = new URLSearchParams({
    period: filters.period, sort: filters.sort, order: filters.order, page: String(filters.page),
  });
  for (const [key, name] of Object.entries(API_PARAMS)) {
    if (filters[key] !== '') p.set(name, filters[key]);
  }
  return '/api/requests?' + p.toString();
}

// ---- drawing ----

function setState(name) {
  $('rq-body').dataset.state = name;
  $('rq-loading').hidden = name !== 'loading';
  $('rq-empty').hidden = name !== 'empty';
  $('rq-error').hidden = name !== 'error';
  $('rq-list').hidden = name !== 'ready';
}

function renderFacets(facets) {
  const { fmtNum } = deps;
  for (const dim of FACETS) {
    const select = $('rq-' + dim);
    const options = (facets[dim] || []).map((o) => {
      const label = dim === 'result' ? (RESULT_LABELS[o.value] || o.value) : o.label;
      return `<option value="${esc(o.value)}">${esc(label)} (${fmtNum(o.count)})</option>`;
    });
    select.innerHTML = '<option value="">All</option>' + options.join('');
    select.value = filters[dim];
  }
}

function requestRow(r) {
  const { fmtCredits, fmtTok, fmtDay, fmtTime } = deps;
  const took = r.elapsed_ms === null ? '' : ' · ' + (r.elapsed_ms / 1000).toFixed(1) + 's';
  return actionRowHtml({
    track: 'request-open',
    data: { sid: r.session_id },
    title: r.message,
    blank: '(no prompt text)',
    meta: splitMeta(
      fmtDay(r.ts) + ' ' + fmtTime(r.ts) + ' · ' + r.project + ' · ' + (r.model_label || r.model)
        + (r.mode ? ' · ' + r.mode : '')
        + ' · ' + fmtTok(r.prompt_tokens) + ' in / ' + fmtTok(r.completion_tokens) + ' out' + took
        + (r.usage_estimated ? ' · estimated' : ''),
      r.credits === null ? 'unbilled' : fmtCredits(r.credits) + ' cr',
      r.error ? '<span class="sev sev-critical">Errored</span>' : '',
    ),
  });
}

function renderSummary(body) {
  const { fmtNum, fmtCredits } = deps;
  const n = body.total;
  $('rq-summary').textContent = n === 0
    ? 'No requests match'
    : fmtNum(n) + (n === 1 ? ' request' : ' requests') + ' · ' + fmtCredits(body.credits) + ' credits';
  $('rq-page').textContent = 'Page ' + body.query.page + ' of ' + body.pages;
  $('rq-prev').disabled = body.query.page <= 1;
  $('rq-next').disabled = body.query.page >= body.pages;
  $('rq-pager').hidden = body.pages <= 1;
}

function render(body) {
  // The server clamps a page past the end; keep the URL on the page actually shown.
  filters.page = body.query.page;
  writeUrl();
  renderFacets(body.facets);
  renderSummary(body);
  $('rq-rows').innerHTML = body.rows.map(requestRow).join('');
  setState(body.rows.length ? 'ready' : 'empty');
}

function activeFilters() {
  return BOX_KEYS.filter((k) => filters[k] !== '').length;
}

function syncControls() {
  $('rq-q').value = filters.q;
  $('rq-min').value = filters.min;
  $('rq-max').value = filters.max;
  $('rq-sort').value = filters.sort;
  const labels = ORDER_LABELS[filters.sort] || ORDER_LABELS.ts;
  $('rq-order').textContent = filters.order === 'asc' ? labels[1] : labels[0];
  $('rq-period').querySelectorAll('button').forEach((b) =>
    b.setAttribute('aria-pressed', String(b.dataset.period === filters.period)));
  const active = activeFilters();
  $('rq-clear').hidden = active === 0;
  $('rq-filter-count').hidden = active === 0;
  $('rq-filter-count').textContent = String(active);
}

// ---- loading and changing the query ----

async function load() {
  const seq = ++loadSeq;
  syncControls();
  writeUrl();
  // Keep the rows on screen while a new page loads; only a first load shows the loading block.
  if ($('rq-list').hidden) setState('loading');
  try {
    const body = await deps.jsonApi(apiUrl());
    if (seq === loadSeq) render(body);
  } catch (exc) {
    if (seq !== loadSeq) return;
    console.error(exc);
    $('rq-summary').textContent = '';
    $('rq-pager').hidden = true;
    setState('error');
  }
}

function change(patch) {
  filters = { ...filters, page: 1, ...patch };
  load();
}

function debouncedChange(patchFor) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => change(patchFor()), SEARCH_DEBOUNCE_MS);
}

function openView(initial) {
  filters = { ...DEFAULTS, period: deps.getPeriod(), ...initial };
  // On a phone the filters start folded so the rows stay in view, unless some are already set.
  $('rq-filter-box').open = window.matchMedia(WIDE_QUERY).matches || activeFilters() > 0;
  if (!$('requests-dialog').open) $('requests-dialog').showModal();
  setState('loading');
  load();
}

// ---- wiring ----

function bindFilters() {
  $('rq-period').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-period]');
    if (btn && btn.dataset.period !== filters.period) change({ period: btn.dataset.period });
  });
  for (const dim of FACETS) {
    $('rq-' + dim).addEventListener('change', (e) => change({ [dim]: e.target.value }));
  }
  $('rq-q').addEventListener('input', (e) => debouncedChange(() => ({ q: e.target.value.trim() })));
  for (const [id, key] of [['rq-min', 'min'], ['rq-max', 'max']]) {
    $(id).addEventListener('input', (e) => debouncedChange(() => ({ [key]: e.target.value.trim() })));
  }
  $('rq-sort').addEventListener('change', (e) => change({ sort: e.target.value, order: 'desc' }));
  $('rq-order').addEventListener('click', () => change({ order: filters.order === 'desc' ? 'asc' : 'desc' }));
  $('rq-clear').addEventListener('click', () =>
    change({ project: '', model: '', mode: '', ide: '', result: '', min: '', max: '' }));
}

function bindPagingAndRows() {
  $('rq-prev').addEventListener('click', () => { filters.page = Math.max(filters.page - 1, 1); load(); });
  $('rq-next').addEventListener('click', () => { filters.page += 1; load(); });
  const list = $('rq-list');
  // The rows are real buttons, so Enter and Space already arrive as a click.
  list.addEventListener('click', (e) => {
    const row = e.target.closest('button[data-sid]');
    if (row) deps.openSession(row.dataset.sid);
  });
}

export function initRequests(handedOver) {
  deps = handedOver;
  $('requests-open').addEventListener('click', () => openView({}));
  $('rq-close').addEventListener('click', () => $('requests-dialog').close());
  $('requests-dialog').addEventListener('close', clearUrl);
  $('rq-retry').addEventListener('click', load);
  bindFilters();
  bindPagingAndRows();
  const fromUrl = readUrl();
  if (fromUrl) openView(fromUrl);
  return { open: openView };
}
