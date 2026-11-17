/* The Sessions "Filters & sort" sheet.
 *
 * The sheet holds the sort and a second copy of the period control; its trigger sits beside the
 * page's period control on the Sessions tab and counts the values that differ from the defaults,
 * so a narrowed or re-sorted list never looks like the whole one. Everything applies as it changes.
 */

export const DEFAULT_PERIOD = 'today';
export const DEFAULT_SORT = 'credits';

export function initFilters({ $, state, periodNames, setPeriod, renderSessions }) {
  function paint() {
    const active = [];
    if (state.period !== DEFAULT_PERIOD) active.push('period ' + (periodNames[state.period] || 'custom range'));
    if (state.sessionsSort !== DEFAULT_SORT) active.push('sort Recent');
    const count = $('filters-count');
    count.hidden = active.length === 0;
    count.textContent = String(active.length);
    $('filters-btn').setAttribute('aria-label', active.length
      ? 'Filters & sort, ' + active.length + ' active: ' + active.join(', ') : 'Filters & sort');
  }

  function setSort(sort) {
    state.sessionsSort = sort;
    $('sessions-sort').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.sort === sort)));
    paint();
    renderSessions();
  }

  $('sessions-sort').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-sort]');
    if (btn) setSort(btn.dataset.sort);
  });
  $('filters-btn').addEventListener('click', () => $('filters-dialog').showModal());
  $('fs-close').addEventListener('click', () => $('filters-dialog').close());
  $('filters-done').addEventListener('click', () => $('filters-dialog').close());
  $('filters-clear').addEventListener('click', () => {
    setSort(DEFAULT_SORT);
    if (state.period !== DEFAULT_PERIOD) {
      state.returnPeriod = null;
      setPeriod(DEFAULT_PERIOD);
    }
  });

  return {
    paint,
    // The trigger belongs to the Sessions tab, the only view with a second control beside the period.
    showFor: (tab) => { $('filters-btn').hidden = tab !== 'sessions'; },
  };
}
