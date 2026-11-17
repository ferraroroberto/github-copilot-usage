/* Model colours and the Settings sheet that merges models and overrides a colour.
 *
 * A model's colour is its provider family's brand colour at an opacity set by its rank among the
 * family's models, in credit order: the most used is solid, then 80, 60, 40 and 20%, with 20% the
 * floor for any further model. A saved override (family and/or opacity) wins over both. The
 * brand colours are lightened where needed to hold 3:1 on the card in each theme; they are not
 * all colour-blind safe together, so the legend, tooltips and the By model table (swatch plus
 * name) carry the identity as well, and stacked segments keep their surface gap.
 */

const BRAND = {
  light: {
    google:    '#4285f4',
    anthropic: '#c15f3c',
    xai:       '#24292f',
    openai:    '#0e9272',
    microsoft: '#b7791f',
    other:     '#8c959f',
  },
  dark: {
    google:    '#5b9bf8',
    anthropic: '#d97757',
    xai:       '#e6edf3',
    openai:    '#10a37f',
    microsoft: '#ffb900',
    other:     '#8b949e',
  },
};
// A plain series palette for charts that are not per model (tokens in / out / cache).
const SERIES = {
  light: ['#0969da', '#bc4c00', '#8250df'],
  dark: ['#4493f8', '#dd6b20', '#a371f7'],
};
// Opacity by rank within a family; a rank past the end keeps the last (the floor).
const RANK_OPACITY = [1, 0.8, 0.6, 0.4, 0.2];
// What the Settings sheet offers as an explicit opacity.
const OPACITIES = RANK_OPACITY;
// Stack order, as the server lists the families.
const FAMILY_ORDER = ['google', 'anthropic', 'xai', 'openai', 'microsoft', 'other'];

/** A plain series colour for charts that are not per model (tokens in / out / cache). */
export function seriesColor(theme, index) {
  return SERIES[theme][index];
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + (n >> 16) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255) + ', ' + alpha + ')';
}

export function createModelColors(getOverrides, getTheme) {
  const slots = new Map();   // label -> insertion order is the credit-order rank in the current data
  let meta = {};

  const info = (label) => meta[label] || { key: label, family: 'other' };

  function style(label) {
    const found = info(label);
    const override = getOverrides()[found.key] || {};
    return { family: override.family || found.family, opacity: override.opacity };
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
    setMeta(next) {
      meta = next || {};
    },
    // Ranks restart from the rows, which arrive in credit order, so the most used model of each
    // family is the solid one; a series only a chart knows about ranks after them.
    seed(rows) {
      slots.clear();
      for (const row of rows) rankOf(row.model);
    },
    colorOf(label) {
      const theme = getTheme();
      const { family, opacity } = style(label);
      const base = BRAND[theme][family] || BRAND[theme].other;
      const alpha = opacity ?? RANK_OPACITY[Math.min(rankOf(label), RANK_OPACITY.length - 1)];
      return alpha < 1 ? withAlpha(base, alpha) : base;
    },
    // Family by family, each family in rank order, so the shades of one provider sit together in a stack.
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
  const opacityOptions = [optionHtml(view, '', 'By usage', override.opacity === undefined)].concat(
    levels.map((o) => optionHtml(view, String(o), Math.round(o * 100) + '%', override.opacity === o)));
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
