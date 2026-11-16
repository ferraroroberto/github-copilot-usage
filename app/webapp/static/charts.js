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

function axisOptions(cssVar, fmtTok, isTok) {
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

function legendOptions(cssVar) {
  return {
    position: 'bottom',
    labels: { color: cssVar('--fg'), boxWidth: 10, boxHeight: 10, padding: 12, font: { size: 11 }, usePointStyle: true },
  };
}

// Options both charts share: one tooltip per bucket, a coverage footer and a tap that focuses the bucket.
function commonOptions(env, ts, scales, label) {
  const { state, cssVar } = env;
  return {
    responsive: true,
    maintainAspectRatio: false,
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
    const sumField = (b, f) => (b.coverage === 'none' ? null
      : Object.values(b.models || {}).reduce((acc, m) => acc + (m[f] || 0), 0));
    const line = (label, field, color, extra) => ({
      label,
      data: ts.map((b) => sumField(b, field)),
      borderColor: color,
      tension: 0.3, borderWidth: 2,
      pointRadius: labels.length > 10 ? 0 : 3, pointHoverRadius: 4,
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
          line('Tokens in', 'prompt_tokens', inColor, { backgroundColor: inColor + '2e', fill: true }),
          line('Tokens out', 'completion_tokens', outColor, { backgroundColor: outColor + '2e', fill: true }),
          line('Cache read (not in tokens in)', 'cache_read_tokens', seriesColor(theme, 2), { borderDash: [5, 4], fill: false }),
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
