/* Settings -> Data sources: the read-model health check.
 *
 * The server (GET and POST /api/health-check) owns the state: whether a run is in flight and the last
 * result, which says measured, incomplete or unavailable and carries only timings and counts. This file
 * paints the row's value and the footnote under the group, and polls while a run is going.
 */

const POLL_MS = 1500;

const name = (c) => c.id.replace(/_/g, ' ');
const ms = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + ' s' : n + ' ms');

function ago(iso, now) {
  const mins = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + ' min ago';
  return mins < 1440 ? Math.round(mins / 60) + ' h ago' : Math.round(mins / 1440) + ' d ago';
}

// The slowest check that counts against the budget, for the sentence under the group.
function slowest(checks) {
  return checks
    .filter((c) => c.enforced && c.median_ms != null)
    .reduce((top, c) => (top === null || c.median_ms > top.median_ms ? c : top), null);
}

export function healthStatus(state, now = Date.now()) {
  if (state.running) return { value: 'Running', tone: null, note: 'Checking a temporary copy of the read model.' };
  const r = state.result;
  if (!r) return { value: 'Not run', tone: null, note: 'Not run yet.' };
  const when = ago(r.ran_at, now);
  if (r.state === 'unavailable') return { value: 'Unavailable', tone: 'attention', note: 'Not measured ' + when + ': ' + r.reason + '.' };
  if (r.state === 'incomplete') {
    return { value: 'Incomplete', tone: 'attention', note: 'Stopped early ' + when + ': ' + r.reason + '. ' + r.checks.length + (r.checks.length === 1 ? ' check' : ' checks') + ' finished.' };
  }
  const failed = r.checks.filter((c) => c.state === 'error');
  const over = r.checks.filter((c) => c.state === 'over_budget');
  const top = slowest(r.checks);
  const size = r.dataset.requests.toLocaleString() + ' requests in ' + r.dataset.sources.toLocaleString() + ' sources';
  if (failed.length) {
    return { value: 'Failed', tone: 'danger', note: failed.length + ' of ' + r.checks.length + ' checks failed ' + when + ': ' + failed.map(name).join(', ') + '.' };
  }
  const slow = top ? ' Slowest: ' + name(top) + ', ' + ms(top.median_ms) + ' of ' + ms(r.budget_ms) + '.' : '';
  if (over.length) {
    return { value: 'Over budget', tone: 'attention', note: over.length + ' of ' + r.checks.length + ' checks over the budget ' + when + ' (' + size + ').' + slow };
  }
  return { value: 'Passed', tone: null, note: r.checks.length + ' checks within the budget ' + when + ' (' + size + ').' + slow };
}

export function initHealthCheck({ $, jsonApi, showToast }) {
  const row = $('health-check');
  let timer = null;

  function paint(state) {
    const { value, tone, note } = healthStatus(state);
    $('health-check-value').textContent = value;
    if (tone) $('health-check-value').dataset.tone = tone;
    else delete $('health-check-value').dataset.tone;
    $('health-check-note').textContent = note;
    row.disabled = state.running;
    if (state.running) row.setAttribute('aria-busy', 'true');
    else row.removeAttribute('aria-busy');
    if (state.running && timer === null) timer = setInterval(load, POLL_MS);
    if (!state.running && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  async function load() {
    try {
      paint(await jsonApi('/api/health-check'));
    } catch (exc) {
      // Without the state the row keeps what it shows; the next load tries again.
      console.error(exc);
    }
  }

  row.addEventListener('click', async () => {
    try {
      paint(await jsonApi('/api/health-check', { method: 'POST' }));
    } catch (exc) {
      console.error(exc);
      if (String(exc.message).endsWith('409')) {
        showToast('A health check is already running.');
        load();
      } else {
        showToast('Could not start the health check: ' + exc.message, 'error');
      }
    }
  });

  return { load };
}
