/* Model colours and the Settings sheet that merges models and overrides a colour.
 *
 * A model's colour is its provider family's hue, stepped lighter (light mode) or darker
 * (dark mode) by its rank among the family's models, in credit order; a model past the third
 * of its family takes the gray. Assignment follows the model, never a filter, and a model
 * keeps its step while the page lives. The family hues are the dashboard's validated
 * categorical order (blue, orange, purple, green, pink) and the steps pass the dataviz
 * validator's ordinal checks per family, with the family boundaries, band and chroma checks
 * in both modes. The lightest light steps and the deepest dark steps sit under 3:1 on the
 * card, so the legend, tooltips and the By model table (swatch plus name) carry the identity
 * as well, and stacked segments keep their surface gap.
 */

const LADDER = {
  light: {
    google:    ['#0969da', '#3489fd', '#74acfd'],
    anthropic: ['#bc4c00', '#de6b31', '#ff8d58'],
    xai:       ['#8250df', '#9f73fe', '#b8a0fd'],
    openai:    ['#1a7f37', '#409e54', '#61be72'],
    other:     ['#bf3989', '#e25aa8', '#fd83c5'],
  },
  dark: {
    google:    ['#4493f8', '#2d7de0', '#1067c8'],
    anthropic: ['#dd6b20', '#c25702', '#a44800'],
    xai:       ['#a371f7', '#8e5bdf', '#7a44c7'],
    openai:    ['#2ea043', '#058a2d', '#007322'],
    other:     ['#db61a2', '#c34b8d', '#ab3478'],
  },
};
const OTHER_COLOR = { light: '#656d76', dark: '#8b949e' };
// Stack order, and the order the single-series charts draw from.
const FAMILY_ORDER = ['google', 'anthropic', 'xai', 'openai', 'other'];
const STEPS_PER_FAMILY = 3;
const OPACITIES = [1, 0.8, 0.6, 0.4];

/** A plain series colour for charts that are not per model (tokens in / out / cache). */
export function seriesColor(theme, index) {
  return LADDER[theme][FAMILY_ORDER[index]][0];
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + (n >> 16) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255) + ', ' + alpha + ')';
}

export function createModelColors(getOverrides, getTheme) {
  const slots = new Map();   // label -> insertion order is the credit-order rank
  let meta = {};

  const info = (label) => meta[label] || { key: label, family: 'other' };

  function style(label) {
    const found = info(label);
    const override = getOverrides()[found.key] || {};
    return { family: override.family || found.family, opacity: override.opacity ?? 1 };
  }

  function rankOf(label) {
    if (!slots.has(label)) slots.set(label, slots.size);
    const family = style(label).family;
    let rank = 0;
    for (const other of slots.keys()) {
      if (other === label) break;
      if (style(other).family === family) rank += 1;
    }
    return rank;
  }

  return {
    // Labels a merge removed free their step for the models that remain.
    setMeta(next) {
      meta = next || {};
      for (const label of [...slots.keys()]) if (!(label in meta)) slots.delete(label);
    },
    // The leading models by credits take the first steps of their families.
    seed(rows) {
      for (const row of rows) rankOf(row.model);
    },
    colorOf(label) {
      const theme = getTheme();
      const { family, opacity } = style(label);
      const rank = rankOf(label);
      const steps = LADDER[theme][family] || LADDER[theme].other;
      const base = rank >= STEPS_PER_FAMILY ? OTHER_COLOR[theme] : steps[rank];
      return opacity < 1 ? withAlpha(base, opacity) : base;
    },
    // Family by family, each family in rank order, so neighbours in a stack are neighbours in a ladder.
    stackOrder(labels) {
      const rank = new Map(labels.map((label) => [label, rankOf(label)]));
      const family = (label) => FAMILY_ORDER.indexOf(style(label).family);
      return [...labels].sort((a, b) => family(a) - family(b) || rank.get(a) - rank.get(b));
    },
  };
}

// ---------------------------------------------------------------------------
// Settings sheet
// ---------------------------------------------------------------------------

export function summarizeModelSettings(cfg) {
  const merged = Object.keys(cfg.model_aliases || {}).length;
  const colored = Object.keys(cfg.model_colors || {}).length;
  const parts = [];
  if (merged) parts.push(merged + ' merged');
  if (colored) parts.push(colored + (colored === 1 ? ' colour' : ' colours'));
  return parts.join(' · ') || 'Default';
}

// What the sheet draws from: the server's catalog, the saved settings and the page's helpers.
const optionHtml = (view, value, text, selected) =>
  `<option value="${view.esc(value)}"${selected ? ' selected' : ''}>${view.esc(text)}</option>`;

function mergeField(view, m, aliases, isTarget) {
  const { catalog, byKey } = view;
  const options = [optionHtml(view, '', 'Keep separate', !aliases[m.key])];
  for (const other of catalog.models) {
    if (other.key === m.key || aliases[other.key]) continue;
    options.push(optionHtml(view, other.key, other.label, aliases[m.key] === other.key));
  }
  // A target written by hand that has no history yet stays selectable, so it can be cleared.
  if (aliases[m.key] && !byKey.has(aliases[m.key])) options.push(optionHtml(view, aliases[m.key], aliases[m.key], true));
  return `
    <label class="rq-label"><span>Merge into</span>
      <select data-track="model-merge" class="select-native" data-key="${view.esc(m.key)}" data-field="merge"${isTarget ? ' disabled' : ''}>${options.join('')}</select>
    </label>`;
}

function colourFields(view, m, override) {
  const family = override.family || '';
  const familyOptions = [optionHtml(view, '', 'Provider default', !family)];
  for (const f of view.catalog.families) familyOptions.push(optionHtml(view, f.id, f.label, family === f.id));
  const levels = OPACITIES.includes(override.opacity) || override.opacity === undefined
    ? OPACITIES : [...OPACITIES, override.opacity].sort((a, b) => b - a);
  const opacityOptions = levels.map((o) =>
    optionHtml(view, o === 1 ? '' : String(o), Math.round(o * 100) + '%', (override.opacity ?? 1) === o));
  return `
    <label class="rq-label"><span>Colour family</span>
      <select data-track="model-family" class="select-native" data-key="${view.esc(m.key)}" data-field="family">${familyOptions.join('')}</select>
    </label>
    <label class="rq-label"><span>Opacity</span>
      <select data-track="model-opacity" class="select-native" data-key="${view.esc(m.key)}" data-field="opacity">${opacityOptions.join('')}</select>
    </label>`;
}

function modelCard(view, m, cfg) {
  const { esc, catalog, byKey } = view;
  const aliases = cfg.model_aliases || {};
  const mergedInto = aliases[m.key];
  const absorbed = catalog.models.filter((o) => aliases[o.key] === m.key).length;
  const spellings = m.raw.length > 1 ? m.raw.length + ' spellings: ' + m.raw.join(', ') : '';
  const note = mergedInto
    ? 'Counted as ' + (byKey.has(mergedInto) ? byKey.get(mergedInto).label : mergedInto)
    : (absorbed ? 'Includes ' + absorbed + (absorbed === 1 ? ' merged model' : ' merged models') : '');
  const swatch = mergedInto ? '' : `<span class="model-dot" style="background:${view.colors.colorOf(m.label)}"></span>`;
  return `
    <li class="mdl">
      <div class="mdl-head">
        <span class="mdl-name">${swatch}${esc(m.label)}</span>
        <span class="muted">${esc(view.fmtNum(m.requests))} ${m.requests === 1 ? 'request' : 'requests'}</span>
      </div>
      ${spellings || note ? `<p class="mdl-sub muted">${esc([spellings, note].filter(Boolean).join(' · '))}</p>` : ''}
      <div class="mdl-grid">
        ${mergeField(view, m, aliases, absorbed > 0)}
        ${mergedInto ? '' : colourFields(view, m, (cfg.model_colors || {})[m.key] || {})}
      </div>
    </li>`;
}

// The next settings value for one changed select.
function nextSetting(cfg, { key, field }, value) {
  if (field === 'merge') {
    const next = { ...(cfg.model_aliases || {}) };
    if (value) next[key] = value; else delete next[key];
    return { name: 'model_aliases', value: next, message: value ? 'Models merged' : 'Model separated' };
  }
  const next = { ...(cfg.model_colors || {}) };
  const entry = { ...(next[key] || {}) };
  if (value) entry[field] = field === 'opacity' ? Number(value) : value;
  else delete entry[field];
  if (Object.keys(entry).length) next[key] = entry; else delete next[key];
  return { name: 'model_colors', value: next, message: 'Colour saved' };
}

export function initModelSettings(deps) {
  const { $, jsonApi, saveSetting, getSettings, showToast } = deps;
  const view = { ...deps, catalog: { families: [], models: [] }, byKey: new Map() };

  function render(focus) {
    const cfg = getSettings();
    view.byKey = new Map(view.catalog.models.map((m) => [m.key, m]));
    $('mdl-empty').hidden = view.catalog.models.length > 0;
    $('mdl-list').innerHTML = view.catalog.models.map((m) => modelCard(view, m, cfg)).join('');
    $('set-models-value').textContent = summarizeModelSettings(cfg);
    if (!focus) return;
    for (const el of $('mdl-list').querySelectorAll('select')) {
      if (el.dataset.key === focus.key && el.dataset.field === focus.field) { el.focus(); break; }
    }
  }

  async function change(select) {
    const { key, field } = select.dataset;
    const next = nextSetting(getSettings(), { key, field }, select.value);
    await saveSetting(next.name, next.value, next.message);
    render({ key, field });
  }

  $('mdl-list').addEventListener('change', (e) => {
    const select = e.target.closest('select[data-field]');
    if (select) change(select);
  });

  $('models-open').addEventListener('click', async () => {
    try {
      view.catalog = await jsonApi('/api/models');
    } catch {
      showToast('Could not load the models. Try again.', 'error');
      return;
    }
    render(null);
    $('models-dialog').showModal();
  });
  $('models-close').addEventListener('click', () => $('models-dialog').close());
}
