/* Background TM import progress on the project page (S6, ADR 0094).
   Polls /jobs/{id}/status for every running import line and rewrites its text and bar.
   Text only (textContent), never markup from the server. */
(function () {
  'use strict';
  var items = Array.from(document.querySelectorAll('.tm-import-job[data-active="1"]'));
  if (!items.length || !window.fetch) return;

  function num(n) {
    n = Number(n) || 0;
    return n.toLocaleString();
  }

  function summary(r) {
    r = r || {};
    var parts = ['done: imported ' + num(r.imported) + ' units',
      'updated ' + num(r.updated), 'skipped ' + num(r.skipped) + ' unchanged'];
    if (r.skipped_refusal) parts.push('skipped ' + num(r.skipped_refusal) + ' refusal(s)');
    if (r.skipped_integrity) {
      parts.push('withheld ' + num(r.skipped_integrity) +
        ' with broken placeholders or inline tags (not saved to TM)');
    }
    if (r.concurrent_deleted) {
      parts.push('did not restore ' + num(r.concurrent_deleted) +
        ' entries deleted while the import ran');
    }
    return parts.join(', ');
  }

  function poll(li) {
    var id = li.getAttribute('data-job-id');
    var status = li.querySelector('.tm-import-status');
    var bar = li.querySelector('.tm-import-bar');
    fetch('/jobs/' + encodeURIComponent(id) + '/status', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (d.status === 'completed') {
          status.textContent = summary(d.result);
          if (bar) bar.remove();
          li.setAttribute('data-active', '0');
          return;
        }
        if (d.status === 'failed' || d.status === 'cancelled') {
          status.textContent = 'failed: ' + (d.error || d.status);
          status.classList.add('error-inline');
          if (bar) bar.remove();
          li.setAttribute('data-active', '0');
          return;
        }
        status.textContent = d.status === 'queued' ? 'waiting to start' : (d.message || 'running');
        if (bar && d.total) {
          bar.max = d.total;
          bar.value = d.done || 0;
        }
        setTimeout(function () { poll(li); }, 2000);
      })
      .catch(function () { setTimeout(function () { poll(li); }, 5000); });
  }

  items.forEach(function (li) { setTimeout(function () { poll(li); }, 1000); });
})();
