/* Collapsible dashboard sections.
 *
 * Every card with a heading gets a chevron that folds its body away. The folded cards are
 * remembered per browser (localStorage), keyed by the card's id or its heading, and a
 * browser without storage simply starts with everything open.
 */

const STORE_KEY = 'copilot-usage.collapsed';
const CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

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

function paint(card, button, heading, folded) {
  card.classList.toggle('collapsed', folded);
  button.setAttribute('aria-expanded', String(!folded));
  button.setAttribute('aria-label', (folded ? 'Expand ' : 'Collapse ') + heading.textContent.trim());
}

export function initSections(root) {
  const folded = readFolded();
  root.querySelectorAll('section.card').forEach((card) => {
    const head = card.querySelector(':scope > .card-head');
    const heading = head && head.querySelector('h2');
    if (!heading) return;
    const key = sectionKey(card, heading);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'icon-btn collapse-btn';
    button.dataset.track = 'section-toggle';
    button.innerHTML = CHEVRON;
    head.appendChild(button);
    paint(card, button, heading, folded.has(key));
    button.addEventListener('click', () => {
      const now = !card.classList.contains('collapsed');
      if (now) folded.add(key); else folded.delete(key);
      paint(card, button, heading, now);
      writeFolded(folded);
    });
  });
}
