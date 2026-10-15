// Background-job progress watcher. Polls /jobs/{id}/status and fills a
// .prog-bar + .job-label inside `el`; reloads the page (minus the ?job param)
// when the job completes so the fresh results render. If the finished job
// (COMPLETED with partial failures, OR FAILED but still carrying
// result.failures) has a non-empty result.failures, it instead reveals a
// "Retry N failed items" button (a .job-retry form in `el`) and skips the
// reload so the button stays put.
function watchJob(jobId, el, opts) {
  opts = opts || {};
  // Localized strings injected by base.html (window.I18N, js.* namespace); each
  // lookup falls back to English so the file works even without the catalog.
  var I = (typeof window !== 'undefined' && window.I18N) || {};
  function tr(key, en) { return (I[key] != null) ? I[key] : en; }
  function fmt(s, n) { return String(s).replace('{n}', n); }
  function plural(base, n, enOne, enOther) {
    return n === 1 ? fmt(tr(base + '_one', enOne), n) : fmt(tr(base + '_other', enOther), n);
  }
  function done() {
    if (opts.reloadOnDone === false) return;
    var u = new URL(window.location.href);
    u.searchParams.delete('job');
    window.location.replace(u.toString());
  }
  function showRetry(n) {
    var form = el.querySelector('.job-retry');
    if (!form) return false;
    var btn = form.querySelector('button');
    // "needing attention" (not "failed"): the set can include blocked/incomplete items,
    // not only hard failures (Contentful).
    if (btn) btn.textContent = plural('retry', n, 'Retry {n} item needing attention',
                                      'Retry {n} items needing attention');
    form.hidden = false;
    return true;
  }
  // A refusal (404 pruned or other-org job, 401 expired session) or a body with no
  // job status is NOT a finished job: show why and stop polling (#545). Network and
  // server errors retry a few times, then stop with a message instead of polling forever.
  var errors = 0;
  var MAX_ERRORS = 5;
  function stopWith(key, en) {
    var label = el.querySelector('.job-label');
    if (label) label.textContent = tr(key, en);
    el.classList.add('job-failed');
  }
  function tick() {
    fetch('/jobs/' + jobId + '/status', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-Loc-Background': '1' }
    })
      .then(function (r) {
        if (r.status === 404) { stopWith('job_not_found', 'This job is no longer available.'); return null; }
        if (r.status === 401) {
          stopWith('job_session_expired', 'Your session expired. Sign in again to see this job.');
          return null;
        }
        if (!r.ok) throw new Error('status ' + r.status);
        return r.json();
      })
      .then(function (j) {
        if (!j) return;
        if (!j.status) throw new Error('no status');
        errors = 0;
        var pct = j.total ? Math.round(100 * j.done / j.total) : (j.status === 'completed' ? 100 : 0);
        var bar = el.querySelector('.prog-bar');
        var label = el.querySelector('.job-label');
        if (bar) bar.style.width = pct + '%';
        var fails = (j.result && j.result.failures) || [];
        // INCOMPLETE / validation-BLOCKED pairs (Contentful) are retryable too and must
        // be visible even when there are zero hard failures (both arrays are absent for
        // other connectors, so this is a no-op for them).
        var incomplete = (j.result && j.result.incomplete) || [];
        var blocked = (j.result && j.result.blocked) || [];
        var attention = fails.length + incomplete.length + blocked.length;
        if (label) {
          var extra = '';
          if (incomplete.length) extra += ' ' + fmt(tr('incomplete_note', '{n} incomplete.'), incomplete.length);
          if (blocked.length) extra += ' ' + fmt(tr('blocked_note', '{n} blocked.'), blocked.length);
          label.textContent =
            j.status === 'queued' ? tr('queued', 'Queued…') :
            j.status === 'running' ? (j.message || tr('running', 'Running…')) :
            j.status === 'failed' ? (tr('failed_prefix', 'Failed: ') + (j.error || tr('unknown_error', 'unknown error'))) :
            (fails.length
              ? (plural('completed_with_failures', fails.length,
                        'Completed with {n} failed item. ', 'Completed with {n} failed items. ') + extra + (j.message || ''))
              : (tr('done', 'Done. ') + extra + (j.message || '')));
        }
        if (j.status === 'completed') {
          if (attention && showRetry(attention)) el.classList.add('job-partial');
          else done();
        } else if (j.status === 'failed') {
          el.classList.add('job-failed');
          // A FAILED run can still carry structured failures (JobFailed) -
          // offer the same targeted retry rather than leaving a dead end.
          if (attention) showRetry(attention);
        } else {
          setTimeout(tick, 1200);
        }
      })
      .catch(function () {
        errors += 1;
        if (errors >= MAX_ERRORS) {
          stopWith('job_status_unavailable', 'Could not read the progress of this job. Reload the page to try again.');
          return;
        }
        setTimeout(tick, 3000);
      });
  }
  tick();
}
