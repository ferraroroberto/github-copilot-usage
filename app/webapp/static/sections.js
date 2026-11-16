/* Collapsible dashboard sections.
 *
 * Every disclosure card (the vendored `<details class="card--collapsible">`) folds its body
 * away from its own summary row. The folded cards are remembered per browser (localStorage),
 * keyed by the card's id or its heading, and a browser without storage simply starts with
 * everything open.
 */

const STORE_KEY = 'copilot-usage.collapsed';

function readFolded() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw.filter((k) => typeof k === 'string') : []);
  } catch { return new Set(); }
}

function writeFolded(folded) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify([...folded])); } catch { /* applies for this view only */ }
}

function sectionKey(card, heading) {
  return card.id || heading.textContent.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

export function initSections(root) {
  const folded = readFolded();
  root.querySelectorAll('details.card--collapsible').forEach((card) => {
    const heading = card.querySelector(':scope > summary .collapse-title');
    if (!heading) return;
    const key = sectionKey(card, heading);
    card.open = !folded.has(key);
    // The summary's click (a tap, Enter or Space) flips `open` after this handler, and the
    // `toggle` event only arrives a frame later, so the new state is derived here and saved at once.
    card.querySelector(':scope > summary').addEventListener('click', () => {
      if (card.open) folded.add(key); else folded.delete(key);
      writeFolded(folded);
    });
  });
}
