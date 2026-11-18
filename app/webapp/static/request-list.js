/* The session sheet's request list.
 *
 * One action-row per request; tapping it opens an inline detail block with every token bucket,
 * both model names, the timing and the stored prompt, so none of it hides behind a hover. The
 * server sends the figures; this module only lays them out.
 */

import { actionRowHtml, esc, splitMeta } from './rows.js';
import { fmtNum, fmtTok } from './tokens.js';

// VS Code's chat log keeps only the start of each prompt (src/vscode_parser.py, _message_text).
const VSCODE_PROMPT_LIMIT = 200;

function figure(r, n) {
  return n === null || n === undefined ? '—' : (r.usage_estimated ? '~' : '') + fmtNum(n);
}

function usageNote(r) {
  if (r.usage_source === 'unavailable') return 'The Copilot CLI recorded no token counts for this request.';
  if (r.usage_estimated) return '~ Estimated: allocated from session totals (' + r.usage_source + ').';
  return '';
}

function promptNote(r) {
  if (r.source === 'vscode' && (r.message || '').length >= VSCODE_PROMPT_LIMIT) {
    return 'VS Code logs keep only the first ' + VSCODE_PROMPT_LIMIT + ' characters of a prompt.';
  }
  return '';
}

function facts(r, fmt) {
  const credits = r.credits === null ? '—' : (r.usage_estimated ? '~' : '') + fmt.fmtCredits(r.credits);
  const took = r.elapsed_ms === null ? '—' : (r.elapsed_ms / 1000).toFixed(1) + 's';
  return [
    ['Model requested', r.model_requested || '—'],
    ['Model used', r.model_label || r.model || '—'],
    ['Mode', r.mode || '—'],
    ['Input volume', figure(r, r.prompt_tokens)],
    ['Context processed', figure(r, r.total_input_tokens)],
    ['Uncached input', figure(r, r.uncached_input_tokens)],
    ['Cache read', figure(r, r.cache_read_tokens)],
    ['Cache write', figure(r, r.cache_write_tokens)],
    ['Output', figure(r, r.completion_tokens)],
    ['Credits', credits],
    ['Took', took],
  ];
}

function detailBlock(r, i, fmt) {
  const grid = facts(r, fmt).map(([label, value]) =>
    `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const items = r.context_items || [];
  const context = items.length
    ? `<h4 class="req-sub">In context · ${fmtNum(items.length)}</h4><ul class="ctx-chips">${
      items.map((name) => `<li title="${esc(name)}">${esc(name)}</li>`).join('')}</ul>`
    : '';
  const note = (text) => (text ? `<p class="footnote">${esc(text)}</p>` : '');
  // The prompt comes first: tapping the row must show what the row's one-line title cut off.
  return `<li class="req-detail" id="sd-req-${i}" hidden>
    <h4 class="req-sub req-sub-first">Prompt</h4>
    <p class="req-prompt">${r.message ? esc(r.message) : '<span class="muted">(no prompt text)</span>'}</p>
    ${note(promptNote(r))}
    <dl class="req-facts">${grid}</dl>
    ${context}${note(usageNote(r))}
  </li>`;
}

function requestRow(r, i, fmt) {
  const tok = (n) => (n === null || n === undefined ? '—' : (r.usage_estimated ? '~' : '') + fmtTok(n));
  const row = actionRowHtml({
    track: 'session-request-toggle',
    data: { req: i },
    expands: 'sd-req-' + i,
    title: r.message,
    blank: '(no prompt text)',
    meta: splitMeta(
      fmt.fmtTime(r.ts) + ' · ' + (r.model_label || r.model)
        + ' · ' + tok(r.prompt_tokens) + ' in / ' + tok(r.completion_tokens) + ' out',
      r.credits === null ? 'unbilled' : (r.usage_estimated ? '~' : '') + fmt.fmtCredits(r.credits) + ' cr',
      r.error ? '<span class="sev sev-critical">Errored</span>' : '',
    ),
  });
  return row + detailBlock(r, i, fmt);
}

export function renderRequestList(listEl, requests, fmt) {
  listEl.innerHTML = requests.map((r, i) => requestRow(r, i, fmt)).join('');
}

// The rows are real buttons: Enter and Space arrive as a click.
export function initRequestList(listEl) {
  listEl.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-req]');
    if (!btn) return;
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    document.getElementById(btn.getAttribute('aria-controls')).hidden = !open;
  });
}
