/* The rename row of the session sheet: a custom name for a session, or a way back to its parsed title.
 *
 * The name is stored by the server in config.json (PUT /api/sessions/{id}/name), never in
 * Copilot's own files; an empty name clears it.
 */

const $ = (id) => document.getElementById(id);

export function initSessionName({ jsonApi, showToast, reopen, refresh }) {
  let sessionId = null;

  async function save(name) {
    const sid = sessionId;
    if (!sid) return;
    try {
      await jsonApi('/api/sessions/' + encodeURIComponent(sid) + '/name', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
    } catch (exc) {
      console.error(exc);
      showToast('Could not save the name. Try again.', true);
      return;
    }
    showToast(name ? 'Session renamed' : 'Original title restored');
    await Promise.all([reopen(sid), refresh()]);
  }

  $('sd-rename').addEventListener('submit', (e) => {
    e.preventDefault();
    save($('sd-name').value.trim());
  });
  $('sd-name-clear').addEventListener('click', () => save(''));

  // Called with each session detail the sheet opens on.
  return function show(sid, detail) {
    sessionId = sid;
    $('sd-name').value = detail.custom_name || '';
    $('sd-name').placeholder = detail.parsed_title || 'Custom name';
    $('sd-name-clear').hidden = !detail.custom_name;
  };
}
