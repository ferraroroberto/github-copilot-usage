/* The one list-row renderer.
 *
 * Every tappable row (a session, an insight, a request, a drill-down entry) is the vendored
 * action-row: one main button that opens a sheet, a one-line title and one status line.
 * Callers supply plain strings; this module escapes them, so no row builds its own markup.
 */

export function esc(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}

// The split status line: words that truncate on one line, optional chips (trusted markup),
// and the figure an ellipsis must never cut.
export function splitMeta(text, tail = '', chips = '') {
  return `<span class="action-row-meta-text">${esc(text)}</span>${chips}`
    + (tail ? `<span class="action-row-meta-tail">${esc(tail)}</span>` : '');
}

// `share` (0-100) draws the drill-down's proportion bar under the status line.
// `expands` names the element id the row shows and hides inline, instead of opening a sheet.
export function actionRowHtml({ track, data = {}, title, blank = '', meta = '', share = null, expands = '' }) {
  const attrs = Object.entries(data).map(([key, value]) => ` data-${key}="${esc(value)}"`).join('');
  const bar = share === null
    ? ''
    : `<span class="mix-track cx-bar"><span class="mix-fill" style="width:${Math.min(100, Math.max(0, share))}%"></span></span>`;
  const popup = expands
    ? ` aria-expanded="false" aria-controls="${esc(expands)}"`
    : ' aria-haspopup="dialog"';
  // dynamic-id-exempt: every call site passes one constant id, so the usage log still sees a fixed set
  const open = `<button type="button" class="action-row-main" data-track="${track}"${attrs}${popup}>`;
  return `
    <li class="action-row">
      ${open}
        <span class="action-row-title" title="${esc(title)}">${title ? esc(title) : `<span class="muted">${esc(blank)}</span>`}</span>
        ${meta ? `<span class="action-row-meta">${meta}</span>` : ''}
        ${bar}
      </button>
    </li>`;
}
