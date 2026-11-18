/* The four trend charts (Chart.js, vendored): credits per model as stacked bars; tokens in / out,
 * tokens per credit as bars; cache read / write as lines.
 *
 * The window drawn is the one the timeline holds (the latest from the summary, or an older one);
 * a bucket the telemetry start date does not reach is a gap, never a zero, and a tap on a
 * bucket hands its dates to the timeline to focus the dashboard on.
 */

/* global Chart */

import { seriesColor } from './models.js';
import { currentTrend, focusBucket } from './timeline.js';

const COVERAGE_NOTE = {
  partial: 'Counted from your telemetry start date only',
  none: 'Before your telemetry start date: not counted',
};

function coverageFooter(items, ts) {
  return COVERAGE_NOTE[(ts[items[0].dataIndex] || {}).coverage] || '';
}

// The caption step of the type scale (--font-caption), in the pixels a canvas wants.
const CHART_FONT = 12;

// Label budget for the x axis: about 4 at phone widths, about 8 at desktop widths.
const PHONE_AXIS = window.matchMedia('(max-width: 560px)');
const tickBudget = () => (PHONE_AXIS.matches ? 4 : 8);

function axisOptions(cssVar, fmtTok, isTok) {
  const grid = cssVar('--border-muted');
  const tick = cssVar('--fg-muted');
  return {
    x: {
      stacked: true,
      grid: { display: false },
      ticks: { color: tick, font: { size: CHART_FONT }, maxRotation: 0, autoSkip: true, maxTicksLimit: tickBudget() },
    },
    y: {
      stacked: true,
      beginAtZero: true,
      grid: { color: grid },
      ticks: { color: tick, font: { size: CHART_FONT }, callback: isTok ? (v) => fmtTok(v) : undefined },
    },
  };
}

function legendOptions(cssVar) {
  return {
    position: 'bottom',
    labels: { color: cssVar('--fg'), boxWidth: 10, boxHeight: 10, padding: 12, font: { size: CHART_FONT }, usePointStyle: true },
  };
}

// A window resize or a rotation re-budgets the x labels without a reload.
function rebudgetTicks(chart) {
  chart.options.scales.x.ticks.maxTicksLimit = tickBudget();
}

// Options every chart shares: one tooltip per bucket, a coverage footer and a tap that focuses the bucket.
// ``hideZero`` leaves a series that holds nothing in a bucket out of that bucket's tooltip;
// ``keepGaps`` keeps an empty bar's tooltip so it can say why the bar is missing.
function commonOptions(env, ts, scales, label, { hideZero = false, keepGaps = false } = {}) {
  const { state, cssVar } = env;
  return {
    responsive: true,
    maintainAspectRatio: false,
    onResize: rebudgetTicks,
    interaction: { mode: 'index', intersect: false },
    // A tap inside the plot area picks the nearest bucket; the legend and the margins stay inert.
    onClick: (evt, _elements, chart) => {
      const area = chart.chartArea;
      if (!area || evt.x < area.left || evt.x > area.right || evt.y < area.top || evt.y > area.bottom) return;
      const hit = chart.getElementsAtEventForMode(evt, 'index', { intersect: false }, true);
      const trend = currentTrend(state);
      if (hit.length && trend) focusBucket(state, trend.time_series[hit[0].index]);
    },
    scales,
    plugins: {
      legend: legendOptions(cssVar),
      tooltip: {
        filter: (item) => (keepGaps || item.parsed.y != null) && !(hideZero && item.parsed.y === 0),
        callbacks: { label, footer: (items) => coverageFooter(items, ts) },
      },
    },
  };
}

// A bucket with no recorded figure is a gap in the line, not a zero.
const gapOr = (b, v) => (b.coverage === 'none' || v == null ? null : v);

// Colour is never the only cue: each line also differs in dash, point shape and fill.
function lineSet(labels, label, data, color, extra) {
  return {
    label,
    data,
    borderColor: color,
    tension: 0.3, borderWidth: 2,
    pointRadius: labels.length > 10 ? 0 : 4, pointHoverRadius: 5,
    ...extra,
  };
}

function lineScales({ cssVar, fmtTok }) {
  const scales = axisOptions(cssVar, fmtTok, true);
  scales.x.stacked = false;
  scales.y.stacked = false;
  return scales;
}

// Each draw replaces the chart it held on the canvas, so a new window or theme never stacks charts.
function mount(env, held, key, canvasId, config) {
  if (held[key]) held[key].destroy();
  held[key] = new Chart(env.$(canvasId), config);
}

function drawCredits(env, held, ts, labels) {
  const { colors, cssVar, fmtCredits, fmtTok } = env;
  const surface = cssVar('--card');
  // Model set across the window, family by family in stable rank order.
  const models = colors.stackOrder([...new Set(ts.flatMap((b) => Object.keys(b.models || {})))]);
  const datasets = models
    .map((m) => ({
      label: m,
      data: ts.map((b) => (b.coverage === 'none' ? null : (b.models?.[m]?.credits) || 0)),
      backgroundColor: colors.colorOf(m),
      borderColor: surface,          // 2px surface gap between stacked segments
      borderWidth: 1,
      borderRadius: 3,
      maxBarThickness: 34,
    }))
    .filter((d) => d.data.some((v) => v > 0));

  mount(env, held, 'credits', 'chart-credits', {
    type: 'bar',
    data: { labels, datasets },
    options: commonOptions(env, ts, axisOptions(cssVar, fmtTok, false),
      (ctx) => ' ' + ctx.dataset.label + ': ' + fmtCredits(ctx.parsed.y) + ' cr', { hideZero: true }),
  });
}

function drawTokens(env, held, ts, labels) {
  const theme = env.currentTheme();
  const sumField = (b, f) => {
    if (b.coverage === 'none') return null;
    const slots = Object.values(b.models || {});
    const known = slots.map((m) => m[f]).filter((v) => v != null);
    if (slots.length && !known.length) return null;
    return known.reduce((acc, v) => acc + v, 0);
  };
  const line = (label, field, color, extra) =>
    lineSet(labels, label, ts.map((b) => sumField(b, field)), color, extra);
  const inColor = seriesColor(theme, 0);
  const outColor = seriesColor(theme, 1);

  mount(env, held, 'tokens', 'chart-tokens', {
    type: 'line',
    data: {
      labels,
      datasets: [
        line('Tokens in', 'prompt_tokens', inColor, { backgroundColor: inColor + '2e', fill: true, pointStyle: 'circle' }),
        line('Tokens out', 'completion_tokens', outColor, { backgroundColor: outColor + '2e', fill: true, pointStyle: 'rectRot', borderDash: [10, 4] }),
        line('Cache read (not in tokens in)', 'cache_read_tokens', seriesColor(theme, 2), { borderDash: [2, 4], fill: false, pointStyle: 'triangle' }),
      ],
    },
    options: commonOptions(env, ts, lineScales(env), tokenLabel(env)),
  });
}

// Why a bucket has no tokens-per-credit bar: nothing billed, or its token counts were not recorded.
function efficiencyGapReason(b) {
  if (b.coverage === 'none') return 'No figure: before your telemetry start date';
  if (!b.credits) return 'No bar: no billed credits in this bucket';
  return 'No bar: token counts were not recorded for this bucket';
}

// The tooltip's lines for one bucket: what the bar divides, then the ratio.
function efficiencyLabel({ fmtTok, fmtCredits }, ts) {
  return (ctx) => {
    const b = ts[ctx.dataIndex];
    if (ctx.parsed.y == null) return ' ' + efficiencyGapReason(b);
    return [
      ' Effective input: ' + fmtTok(b.effective_input_tokens),
      ' Effective output: ' + fmtTok(b.effective_output_tokens),
      ' Credits: ' + fmtCredits(b.credits),
      ' Tokens per credit: ' + fmtTok(ctx.parsed.y),
    ];
  };
}

// Effective input + output per credit, one bar per bucket in the "Tokens in" colours; a bucket
// with no figure is left empty, never drawn as zero.
function drawEfficiency(env, held, ts, labels) {
  const color = seriesColor(env.currentTheme(), 0);
  mount(env, held, 'efficiency', 'chart-efficiency', {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Tokens per credit',
        data: ts.map((b) => gapOr(b, b.tokens_per_credit)),
        backgroundColor: color + '2e',
        borderColor: color,
        borderWidth: 2,
        borderRadius: 3,
        maxBarThickness: 34,
      }],
    },
    options: commonOptions(env, ts, axisOptions(env.cssVar, env.fmtTok, true),
      efficiencyLabel(env, ts), { keepGaps: true }),
  });
}

function drawCache(env, held, ts, labels) {
  const theme = env.currentTheme();
  mount(env, held, 'cache', 'chart-cache', {
    type: 'line',
    data: {
      labels,
      datasets: [
        lineSet(labels, 'Cache read', ts.map((b) => gapOr(b, b.cache_read_tokens)), seriesColor(theme, 2),
          { borderDash: [2, 4], fill: false, pointStyle: 'triangle' }),
        lineSet(labels, 'Cache write', ts.map((b) => gapOr(b, b.cache_write_tokens)), seriesColor(theme, 1),
          { fill: false, pointStyle: 'rect' }),
      ],
    },
    options: commonOptions(env, ts, lineScales(env), tokenLabel(env)),
  });
}

const tokenLabel = ({ fmtTok }) => (ctx) => ' ' + ctx.dataset.label + ': ' + fmtTok(ctx.parsed.y);

// Returns the function that (re)draws every chart from the current trend.
export function createCharts(env) {
  const { $, state } = env;
  const held = {};

  return function renderCharts() {
    const trend = currentTrend(state);
    const ts = (trend && trend.time_series) || [];
    const card = $('charts-card');
    card.hidden = !ts.length;
    if (!ts.length) return;
    const labels = ts.map((b) => b.label);
    drawCredits(env, held, ts, labels);
    drawTokens(env, held, ts, labels);
    drawEfficiency(env, held, ts, labels);
    drawCache(env, held, ts, labels);
  };
}
