/* Queued rows use short status reads; SSE begins only after admission. */
(() => {
  const node = document.querySelector('[data-queued-run]');
  if (!node) return;
  const I = window.I18N || {};
  function tr(key, en) { return I[key] != null ? I[key] : en; }
  async function poll() {
    if (document.visibilityState === 'visible') {
      try {
        const response = await fetch(`/runs/${node.dataset.queuedRun}/status.json`, {
          credentials: 'same-origin',
          headers: { Accept: 'application/json', 'X-Loc-Background': '1' }
        });
        // A pruned run (404), a refused run (403) or an expired session (401) will never
        // become admitted: say so and stop polling instead of waiting silently (#545).
        if (response.status === 404 || response.status === 403) {
          node.textContent = tr('job_not_found', 'This job is no longer available.');
          return;
        }
        if (response.status === 401) {
          node.textContent = tr('job_session_expired', 'Your session expired. Sign in again to see this job.');
          return;
        }
        if (response.ok) {
          const run = await response.json();
          if (run.status && run.status !== 'queued') {
            window.location.reload();
            return;
          }
        }
      } catch (_) { /* A transient refusal leaves the visible queued state intact. */ }
    }
    window.setTimeout(poll, 5000);
  }
  window.setTimeout(poll, 5000);
})();
