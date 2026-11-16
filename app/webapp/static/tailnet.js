/* Settings -> Available features: the opt-in to open the dashboard from other devices on the owner's
 * Tailscale network.
 *
 * The server (GET and PUT /api/tailnet) owns the state: whether the option is saved, whether this server
 * is serving it right now, the address to open, and why it is not. The bind is made when the server
 * starts, so a switch flipped here says it applies after a restart instead of pretending to be live.
 */

import { setSwitch } from './_vendored/switch/switch.js';

const $ = (id) => document.getElementById(id);

// The row's hint is one truncated line on a phone, so it only says the state; the footnote wraps and
// carries the sentence and the address.
const WHAT = 'Open the dashboard from other devices on your Tailscale network.';

export function tailnetStatus(state) {
  if (!state.enabled) {
    return state.live
      ? { hint: 'Off after restart', note: WHAT + ' Off after the next restart.' }
      : { hint: 'Off', note: WHAT + ' Off: this machine answers on 127.0.0.1 only.' };
  }
  if (state.live) return { hint: 'On', note: WHAT + ' Open ' + state.url };
  if (state.restart_needed) return { hint: 'On after restart', note: WHAT + ' Takes effect when the server restarts.' };
  return {
    hint: 'Unavailable here',
    note: WHAT + ' Not available, staying on 127.0.0.1: ' + (state.reason || 'no reason was reported') + '.',
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
