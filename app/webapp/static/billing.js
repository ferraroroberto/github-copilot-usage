/* The official billing card (Overview): GET /api/billing, account-wide spend for the last 14 days.
 *
 * The server decides the mode (src/billing_state.py); this only presents it. A card that is not
 * ready or partial shows the server's reason, never the empty-data sentence.
 */

import { esc } from './rows.js';

// Billing changes server-side at most every 5 min (src/billing.py caches today),
// so the 30 s refresh need not re-ask each time; 60 s is a stated guess, not a
// measurement, and is deliberately shorter than that cache.
const BILLING_POLL_MS = 60_000;

export function initBilling({ $, jsonApi, isOn, setNote, fmtTime, fmtCredits, fmtUsd }) {
  let fetchedAt = 0;

  function render(body) {
    const rows = body.daily || [];
    const missing = (body.failed_days || []).length;
    const tbody = $('billing-table').querySelector('tbody');
    $('billing-asof').textContent = body.as_of ? 'as of ' + fmtTime(body.as_of) : '';
    const missingNote = missing
      ? ' ' + missing + (missing === 1 ? ' day' : ' days') + ' could not be fetched.'
      : '';
    if (!rows.length) {
      $('billing-wrap').hidden = true;
      setNote($('billing-note'), body.available
        ? 'No spend recorded in this window.' + missingNote
        : (body.reason || 'Not available.'));
      return;
    }
    $('billing-wrap').hidden = false;
    const total = rows.reduce((acc, r) => acc + (Number(r.credits) || 0), 0);
    const totalUsd = rows.reduce((acc, r) => acc + (Number(r.usd) || 0), 0);
    setNote($('billing-note'),
      'Account-wide official spend (all devices), last 14 days: '
      + fmtCredits(total) + ' credits ≈ ' + fmtUsd(totalUsd) + '.' + missingNote);
    tbody.innerHTML = rows.map((r) => `
      <tr>
        <td>${esc(r.date)}</td>
        <td class="td-trunc">${esc(r.model)}</td>
        <td class="num">${fmtCredits(r.credits)}</td>
        <td class="num">${fmtUsd(r.usd)}</td>
      </tr>`).join('');
  }

  async function fetchBilling() {
    if (!isOn()) return;
    if (Date.now() - fetchedAt < BILLING_POLL_MS) return;
    fetchedAt = Date.now();
    let body;
    try {
      body = await jsonApi('/api/billing');
    } catch (exc) {
      // The card could not tell: say so, and let the next 30 s refresh retry.
      fetchedAt = 0;
      $('billing-wrap').hidden = true;
      $('billing-asof').textContent = '';
      setNote($('billing-note'), 'Could not load billing data from the dashboard server.');
      console.error(exc);
      return;
    }
    render(body);
  }

  // A feature switch re-reads the card at once instead of waiting out the interval.
  return { fetch: fetchBilling, reset: () => { fetchedAt = 0; } };
}
