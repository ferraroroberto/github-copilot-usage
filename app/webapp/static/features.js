/* Settings -> Available features: the per-machine switches for the app's feature blocks.
 *
 * The server (GET /api/features, PUT /api/features/{id}) owns the state, what each block needs and
 * whether this machine has it. This module only draws it: a switch per row, the reason under the
 * group when a block cannot run here, and `data-feature-off` on every element that belongs to a
 * block that is off (styles.css hides those). A block that is off is never asked for data by the page.
 */

import { setSwitch } from './_vendored/switch/switch.js';

const $ = (id) => document.getElementById(id);

export function initFeatures({ jsonApi, showToast, afterToggle }) {
  let blocks = [];
  const listeners = [];

  const find = (id) => blocks.find((b) => b.id === id);

  function paintRow(btn, block) {
    setSwitch(btn, block.enabled);
    const hint = btn.closest('.settings-row').querySelector('.settings-row-hint');
    hint.textContent = block.available ? hint.dataset.covers : 'Unavailable on this machine';
    hint.title = block.available ? '' : block.reason;
  }

  function paint() {
    document.querySelectorAll('[data-feature]').forEach((btn) => {
      const block = find(btn.dataset.feature);
      if (block) paintRow(btn, block);
    });
    document.querySelectorAll('[data-feature-ui]').forEach((el) => {
      const block = find(el.dataset.featureUi);
      el.toggleAttribute('data-feature-off', !!block && !block.enabled);
    });
    const missing = blocks.filter((b) => !b.available);
    $('features-note').textContent = missing.map((b) => b.label + ': ' + b.reason + '.').join(' ');
    $('features-note').hidden = !missing.length;
    listeners.forEach((fn) => fn());
  }

  function apply(next) {
    blocks = next;
    paint();
  }

  async function load() {
    try {
      apply((await jsonApi('/api/features')).blocks);
    } catch (exc) {
      // Without the list every block counts as on, which is what an unconfigured machine has.
      console.error(exc);
    }
  }

  document.querySelectorAll('[data-feature]').forEach((btn) => btn.addEventListener('click', async () => {
    const block = find(btn.dataset.feature);
    if (!block) return;
    const next = !block.enabled;
    setSwitch(btn, next);
    try {
      apply((await jsonApi('/api/features/' + encodeURIComponent(block.id), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      })).blocks);
      showToast(block.label + (next ? ' on' : ' off'));
      afterToggle();
    } catch (exc) {
      console.error(exc);
      setSwitch(btn, block.enabled);
      showToast('Could not save that setting. Try again.', 'error');
    }
  }));

  return {
    load,
    // A block the server has not reported yet counts as on.
    isOn: (id) => { const block = find(id); return !block || block.enabled; },
    onChange: (fn) => { listeners.push(fn); },
  };
}
