/* The page's address as the record of what is on show, so the browser's Back and Forward move
 * between views instead of leaving the app.
 *
 * Two things ride the query string: the dashboard's period or date-range focus (`dash`, left out
 * when it is the default) and, while the request explorer is open, its own state (`view=requests`
 * plus its filters, sort and page). The explorer composes its part; the dashboard composes the
 * rest. A commit adds a history entry (`push`), rewrites the current one (`replace`, for typing in
 * a text box) or only reads (`none`, when the address already says it, as after Back).
 */

import { DEFAULT_PERIOD } from './filters.js';

const DASH_PERIODS = ['today', 'week', 'month', 'cycle', 'all'];
const RANGE = /^range:\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$/;

/** The dashboard period in the address, or null when it is absent or not a period this page knows. */
export function readDash() {
  const raw = new URLSearchParams(window.location.search).get('dash');
  return raw !== null && (DASH_PERIODS.includes(raw) || RANGE.test(raw)) ? raw : null;
}

/** The address for a dashboard period plus, when the explorer is open, its URLSearchParams. */
export function composeUrl(dash, explorerParams) {
  const p = new URLSearchParams(explorerParams || undefined);
  if (dash && dash !== DEFAULT_PERIOD) p.set('dash', dash);
  const query = p.toString();
  return window.location.pathname + (query ? '?' + query : '');
}

/** Move the address to `url`; a url the address already holds adds nothing. */
export function commitUrl(url, mode) {
  if (mode === 'none' || url === window.location.pathname + window.location.search) return;
  if (mode === 'push') history.pushState(null, '', url);
  else history.replaceState(null, '', url);
}
