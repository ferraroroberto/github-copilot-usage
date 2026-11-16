/* Settings -> Available features: the opt-in to open the dashboard from other devices on the owner's
 * Tailscale network.
 *
 * The server (GET and PUT /api/tailnet) owns the state: whether the option is saved, whether this server
 * is serving it right now, the address to open, and why it is not. The bind is made when the server
 * starts, so a switch flipped here says it applies after a restart instead of pretending to be live.
 */

import { setSwitch } from './_vendored/switch/switch.js';

const $ = (id) => document.getElementById(id);

const OFF_HINT = 'Off: this machine answers on 127.0.0.1 only';

export function tailnetStatus(state) {
  if (!state.enabled) {
    return state.live
      ? { hint: 'Off after the next restart', note: '' }
      : { hint: OFF_HINT, note: '' };
  }
  if (state.live) return { hint: 'On: ' + state.url, note: '' };
  if (state.restart_needed) return { hint: 'On after the server restarts', note: '' };
  return {
    hint: 'On, but not available: staying on 127.0.0.1',
    note: 'Open from other devices: ' + (state.reason || 'no reason was reported') + '.',
  };
}

export function initTailnet({ jsonApi, showToast }) {
  const btn = $('tailnet-switch');
  let current = null;

  function paint(state) {
    current = state;
    const { hint, note } = tailnetStatus(state);
    setSwitch(btn, state.enabled);
    $('tailnet-hint').textContent = hint;
    $('tailnet-note').textContent = note;
    $('tailnet-note').hidden = !note;
  }

  async function load() {
    try {
      paint(await jsonApi('/api/tailnet'));
    } catch (exc) {
      // Without the state the row keeps its off look, which is what the server does by default.
      console.error(exc);
    }
  }

  btn.addEventListener('click', async () => {
    if (!current) return;
    const next = !current.enabled;
    setSwitch(btn, next);
    try {
      paint(await jsonApi('/api/tailnet', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      }));
      showToast(next ? 'Tailscale access on after the next restart' : 'Tailscale access off after the next restart');
    } catch (exc) {
      console.error(exc);
      setSwitch(btn, current.enabled);
      showToast('Could not save that setting. Try again.', 'error');
    }
  });

  return { load };
}
