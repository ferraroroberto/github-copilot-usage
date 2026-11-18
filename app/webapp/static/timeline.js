/* The dashboard's time controls: stepping the trend charts through older windows, focusing every
 * figure on a tapped bucket's dates, and saying how much of a period the telemetry start date covers.
 *
 * The server owns the windows and the coverage (GET /api/trends, the summary's `coverage`); this
 * module holds only which window is open and draws the controls around it.
 */

import { periodLabel } from './insights.js';

const $ = (id) => document.getElementById(id);

let deps = null;     // the dashboard state and the callbacks it hands over

export function isFocused(period) {
  return typeof period === 'string' && period.startsWith('range:');
}

// What the charts draw: the summary's own latest window, or the older one fetched for the offset.
export function currentTrend(state) {
  return state.trend || state.summary;
}

// The older window for the offset in state, or null when the latest one (already in the summary) is open.
export function fetchTrend(state) {
  if (!state.trendOffset) return Promise.resolve(null);
  return deps.jsonApi('/api/trends?period=' + encodeURIComponent(state.period) + '&offset=' + state.trendOffset);
}

function fmtDate(iso) {
  const [y, m, d] = String(iso).split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function rangeText(win) {
  if (!win) return '';
  return win.from === win.to ? fmtDate(win.from) : fmtDate(win.from) + ' – ' + fmtDate(win.to);
}

// A tap on a bucket narrows the whole dashboard to the days it spans.
export function focusBucket(state, bucket) {
  if (!bucket || bucket.coverage === 'none') return;
  const period = 'range:' + bucket.date + '..' + bucket.end;
  if (period === state.period) return;
  if (!isFocused(state.period)) state.returnPeriod = state.period;
  deps.setPeriod(period);
}

async function stepTo(state, offset) {
  const before = state.trendOffset;
  state.trendOffset = Math.max(0, offset);
  try {
    state.trend = await fetchTrend(state);
  } catch (exc) {
    console.error(exc);
    state.trendOffset = before;
    deps.showToast('Could not load that window. Try again.', 'error');
    return;
  }
  deps.renderCharts();
  render(state);
}

export function render(state) {
  const focused = isFocused(state.period);
  const trend = currentTrend(state);
  const win = trend && trend.trend_window;

  $('focus-bar').hidden = !focused;
  if (focused) {
    $('focus-label').textContent = 'Focused on ' + periodLabel(state.period);
    const back = periodLabel(state.returnPeriod || 'week');
    $('focus-clear').textContent = 'Back to ' + back.charAt(0).toLowerCase() + back.slice(1);
  }

  $('trend-nav').hidden = focused;
  $('trend-range').textContent = rangeText(win);
  $('trend-hint').textContent = focused
    ? 'Tap a bar or point to narrow the focus to that bucket.'
    : 'Tap a bar or point to focus the dashboard on those dates.';
  $('trend-older').disabled = !(win && win.has_older);
  $('trend-newer').disabled = !(win && win.has_newer);
  $('trend-latest').disabled = !state.trendOffset;

  renderCoverage(state.summary && state.summary.coverage);
}

function renderCoverage(coverage) {
  const note = $('coverage-note');
  const state = coverage && coverage.state;
  note.hidden = !(state === 'partial' || state === 'none');
  if (note.hidden) return;
  const from = fmtDate(coverage.start_date);
  note.textContent = state === 'none'
    ? 'No telemetry counted for this period: it ends before your telemetry start date, ' + from + '.'
    : 'Partial coverage: counting from ' + from + ', your telemetry start date. Earlier requests are left out.';
}

export function initTimeline(d) {
  deps = d;
  const { state } = d;
  $('trend-older').addEventListener('click', () => stepTo(state, state.trendOffset + 1));
  $('trend-newer').addEventListener('click', () => stepTo(state, state.trendOffset - 1));
  $('trend-latest').addEventListener('click', () => stepTo(state, 0));
  $('focus-clear').addEventListener('click', () => {
    const back = state.returnPeriod || 'week';
    state.returnPeriod = null;
    deps.setPeriod(back);
  });
}
