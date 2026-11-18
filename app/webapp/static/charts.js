/* The two trend charts (Chart.js, vendored): credits per model as stacked bars, tokens in / out as lines.
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

// Options both charts share: one tooltip per bucket, a coverage footer and a tap that focuses the bucket.
function commonOptions(env, ts, scales, label) {
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
      tooltip: { callbacks: { label, footer: (items) => coverageFooter(items, ts) } },
    },
  };
}

// Returns the function that (re)draws both charts from the current trend.
export function createCharts(env) {
  const { $, state, colors, currentTheme, cssVar, fmtCredits, fmtTok } = env;
  let creditChart = null;
  let tokenChart = null;

  function drawCredits(ts, labels) {
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

    if (creditChart) creditChart.destroy();
    creditChart = new Chart($('chart-credits'), {
      type: 'bar',
      data: { labels, datasets },
      options: commonOptions(env, ts, axisOptions(cssVar, fmtTok, false),
        (ctx) => ' ' + ctx.dataset.label + ': ' + fmtCredits(ctx.parsed.y) + ' cr'),
    });
  }

  function drawTokens(ts, labels) {
    const theme = currentTheme();
    // A bucket whose requests recorded no such figure is a gap in the line, not a zero.
    const sumField = (b, f) => {
      if (b.coverage === 'none') return null;
      const slots = Object.values(b.models || {});
      const known = slots.map((m) => m[f]).filter((v) => v != null);
      if (slots.length && !known.length) return null;
      return known.reduce((acc, v) => acc + v, 0);
    };
    // Colour is never the only cue: each line also differs in dash, point shape and fill.
    const line = (label, field, color, extra) => ({
      label,
      data: ts.map((b) => sumField(b, field)),
      borderColor: color,
      tension: 0.3, borderWidth: 2,
      pointRadius: labels.length > 10 ? 0 : 4, pointHoverRadius: 5,
      ...extra,
    });
    const inColor = seriesColor(theme, 0);
    const outColor = seriesColor(theme, 1);
    const scales = axisOptions(cssVar, fmtTok, true);
    scales.x.stacked = false;
    scales.y.stacked = false;

    if (tokenChart) tokenChart.destroy();
    tokenChart = new Chart($('chart-tokens'), {
      type: 'line',
      data: {
        labels,
        datasets: [
          line('Tokens in', 'prompt_tokens', inColor, { backgroundColor: inColor + '2e', fill: true, pointStyle: 'circle' }),
          line('Tokens out', 'completion_tokens', outColor, { backgroundColor: outColor + '2e', fill: true, pointStyle: 'rectRot', borderDash: [10, 4] }),
          line('Cache read (not in tokens in)', 'cache_read_tokens', seriesColor(theme, 2), { borderDash: [2, 4], fill: false, pointStyle: 'triangle' }),
        ],
      },
      options: commonOptions(env, ts, scales, (ctx) => ' ' + ctx.dataset.label + ': ' + fmtTok(ctx.parsed.y)),
    });
  }

  return function renderCharts() {
    const trend = currentTrend(state);
    const ts = (trend && trend.time_series) || [];
    const card = $('charts-card');
    card.hidden = !ts.length;
    if (!ts.length) return;
    const labels = ts.map((b) => b.label);
    drawCredits(ts, labels);
    drawTokens(ts, labels);
  };
}
